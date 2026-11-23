"""Run model-written code in a container: no network, the case read-only.

Off unless ``HARNESS_SANDBOX=docker``. Each ``exec`` is one ``docker run``:

- the runtime is ``HARNESS_SANDBOX_RUNTIME`` (default ``runsc``, gVisor), and the
  sandbox refuses to start when Docker does not list it. A plain ``runc``
  container shares the host kernel, so it runs only when named on purpose;
- no network, every capability dropped, no new privileges, a read-only root,
  and limits on memory, CPU and processes;
- the session workspace is the only writable mount, at ``/workspace``; the case
  folder is at ``/case``, read-only;
- a run that passes the timeout is killed twice over: ``timeout -s KILL`` inside
  the container stops the command, and the named container is removed from the
  host if it is still there. Killing the ``docker`` client alone would leave
  the container running;
- Python comes from the ``dicom-sandbox`` pixi environment, mounted read-only at
  its own path, so no image has to be built: numpy, scipy, SimpleITK, nibabel,
  pydicom, scikit-image, pandas and matplotlib.

Settings: HARNESS_SANDBOX, HARNESS_SANDBOX_RUNTIME, HARNESS_SANDBOX_IMAGE
(default debian:bookworm-slim), HARNESS_SANDBOX_PYTHON_ENV, HARNESS_SANDBOX_MEMORY
(default 4g), HARNESS_SANDBOX_CPUS (default 2), HARNESS_SANDBOX_TIMEOUT (seconds
per command, default 300).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from meddataos.core.workspace import REPO

MAX_OUTPUT_CHARS = 8000
# A preview or figure sent back to the model; anything larger is refused.
MAX_IMAGE_BYTES = 4 * 1024 * 1024
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_CASE = "/case"
# Extra seconds the host waits beyond the timeout, so the in-container kill
# (exit 137) normally arrives first and the host kill is only the fallback.
HOST_GRACE_SECONDS = 15
KILLED_EXIT_CODE = 137


@dataclass(frozen=True)
class SandboxSettings:
    """How the sandbox runs, from the environment."""

    backend: str
    runtime: str
    image: str
    python_env: Path
    memory: str
    cpus: str
    timeout: float

    @classmethod
    def from_env(cls) -> SandboxSettings:
        """Settings from HARNESS_SANDBOX_* variables."""
        default_env = REPO / ".pixi" / "envs" / "dicom-sandbox"
        return cls(
            backend=os.getenv("HARNESS_SANDBOX", "off").strip().lower(),
            runtime=os.getenv("HARNESS_SANDBOX_RUNTIME", "runsc").strip(),
            image=os.getenv("HARNESS_SANDBOX_IMAGE", "debian:bookworm-slim").strip(),
            python_env=Path(
                os.getenv("HARNESS_SANDBOX_PYTHON_ENV", str(default_env))
            ).expanduser(),
            memory=os.getenv("HARNESS_SANDBOX_MEMORY", "4g"),
            cpus=os.getenv("HARNESS_SANDBOX_CPUS", "2"),
            timeout=float(os.getenv("HARNESS_SANDBOX_TIMEOUT", "300")),
        )

    @property
    def enabled(self) -> bool:
        """Whether sandbox tools are offered at all."""
        return self.backend == "docker"


def unavailable_reason(settings: SandboxSettings) -> str | None:
    """Why exec cannot run here, or None when it can."""
    if not settings.enabled:
        return "the sandbox is off (set HARNESS_SANDBOX=docker)"

    if shutil.which("docker") is None:
        return "docker is not installed"

    if not (settings.python_env / "bin" / "python").exists():
        return (
            f"no sandbox Python at {settings.python_env}; "
            "run: pixi install -e dicom-sandbox"
        )

    return runtime_problem(settings)


def runtime_problem(settings: SandboxSettings) -> str | None:
    """Why no container can be started with the sandbox's runtime, or None.

    MCP servers that run in a container use this too: they need Docker and the
    runtime, but not HARNESS_SANDBOX or the exec Python.
    """
    if shutil.which("docker") is None:
        return "docker is not installed"

    runtimes = docker_runtimes()
    if settings.runtime not in runtimes:
        listed = ", ".join(sorted(runtimes)) or "none"
        return (
            f"Docker has no {settings.runtime!r} runtime (it lists: {listed}). "
            "Install gVisor, or name another runtime in HARNESS_SANDBOX_RUNTIME "
            "knowing what isolation it gives."
        )

    return None


def docker_runtimes() -> set[str]:
    """The container runtimes Docker lists."""
    try:
        listed = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        return set(json.loads(listed.stdout or "{}"))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return set()


def docker_command(
    settings: SandboxSettings,
    workspace: Path,
    case_root: Path | None,
    command: str,
    name: str = "",
) -> list[str]:
    """The ``docker run`` that executes one shell command in the sandbox.

    ``name`` names the container so the host can remove it if it outlives the
    timeout.
    """
    environment = settings.python_env.resolve()
    arguments = [
        "docker",
        "run",
        "--rm",
        *([f"--name={name}"] if name else []),
        *isolation_flags(settings),
        "--tmpfs=/tmp:rw,size=256m",
        f"--memory={settings.memory}",
        f"--cpus={settings.cpus}",
        "--pids-limit=256",
        f"--volume={workspace.resolve()}:{CONTAINER_WORKSPACE}:rw",
        # The environment sits at its own path: conda prefixes are baked into it.
        f"--volume={environment}:{environment}:ro",
        f"--workdir={CONTAINER_WORKSPACE}",
        f"--env=PATH={environment}/bin:/usr/local/bin:/usr/bin:/bin",
        "--env=MPLBACKEND=Agg",
        "--env=HOME=/tmp",
        "--env=PYTHONDONTWRITEBYTECODE=1",
    ]

    if case_root is not None:
        arguments.append(f"--volume={case_root.resolve()}:{CONTAINER_CASE}:ro")

    # KILL, not TERM: a script that traps TERM must still stop.
    limit = ["timeout", "-s", "KILL", f"{max(1, int(settings.timeout))}"]
    return [*arguments, settings.image, *limit, "/bin/sh", "-c", command]


def isolation_flags(settings: SandboxSettings) -> list[str]:
    """What every harness container gets: the runtime, no network, no privileges.

    Files it writes belong to the user who runs the server.
    """
    return [
        f"--runtime={settings.runtime}",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        f"--user={os.getuid()}:{os.getgid()}",
    ]


def run_command(
    settings: SandboxSettings, workspace: Path, case_root: Path | None, command: str
) -> dict:
    """Run one command and return its exit code and (trimmed) output."""
    reason = unavailable_reason(settings)

    if reason:
        return {"error": reason}

    for folder in ("scripts", "out"):
        (workspace / folder).mkdir(parents=True, exist_ok=True)

    name = f"meddataos-exec-{uuid4().hex[:12]}"
    too_long = {
        "error": f"the command ran past {settings.timeout:.0f} s and was stopped"
    }
    started = time.monotonic()

    try:
        completed = subprocess.run(
            docker_command(settings, workspace, case_root, command, name),
            capture_output=True,
            text=True,
            timeout=settings.timeout + HOST_GRACE_SECONDS,
        )
    except subprocess.TimeoutExpired:
        remove_container(name)
        return too_long

    # 137 is also an out-of-memory kill; only a run that used the whole time
    # limit was stopped by the timeout.
    ran_for = time.monotonic() - started
    if completed.returncode == KILLED_EXIT_CODE and ran_for >= settings.timeout:
        return too_long

    return {
        "exit_code": completed.returncode,
        "stdout": _trim(completed.stdout),
        "stderr": _trim(completed.stderr),
    }


def remove_container(name: str) -> None:
    """Kill and remove a container that outlived its timeout, if it is there."""
    try:
        subprocess.run(
            ["docker", "rm", "--force", name],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Nothing more the server can do; the name is in the log line.
        pass


def image_for_model(workspace: Path, relative_path: str) -> dict:
    """A workspace image as base64, for a message the model can look at."""
    import base64

    path = (workspace / relative_path).resolve()

    # Only files inside this session's workspace can be shown.
    if not path.is_relative_to(workspace.resolve()) or not path.is_file():
        return {"error": f"{relative_path} is not a file in the workspace"}

    mime_type = IMAGE_TYPES.get(path.suffix.lower())
    if mime_type is None:
        return {"error": f"{path.suffix or 'that file'} is not a PNG or JPEG"}

    data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        return {"error": f"{relative_path} is larger than {MAX_IMAGE_BYTES} bytes"}

    return {
        "path": relative_path,
        "mime_type": mime_type,
        "image_base64": base64.b64encode(data).decode("ascii"),
    }


def _trim(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text

    # Keep the end: errors and results are usually printed last.
    return (
        f"[... {len(text) - MAX_OUTPUT_CHARS} characters cut ...]\n"
        + text[-MAX_OUTPUT_CHARS:]
    )
