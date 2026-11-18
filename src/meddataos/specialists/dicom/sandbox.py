"""The DICOM specialist's sandbox: an Agents SDK sandbox over one workspace per consult.

Each consult (conversation key) gets ``$SPECIALISTS_CACHE/dicom/workspaces/<key>/``.
It holds the volumes and previews MedDataOS returned plus whatever the agent writes,
and persists across the POSTs of one consult.

``DICOM_SANDBOX=local`` (the default) uses the SDK's ``UnixLocalSandboxClient``.
Commands run on this machine with the ``dicom-sandbox`` pixi environment first on
PATH and API keys removed from the environment. It is not a security boundary:
use it on your own machine only. ``DICOM_SANDBOX=docker`` is reserved for the
Docker client.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Literal

from agents.run_config import SandboxRunConfig
from agents.sandbox import Manifest
from agents.sandbox.capabilities import Capability, Shell
from agents.sandbox.capabilities.tools import ViewImageTool
from agents.sandbox.manifest import Environment
from agents.tool import Tool

from meddataos.specialists.common import CACHE_DIR, REPO

WORKSPACES = CACHE_DIR / "dicom" / "workspaces"
SANDBOX_ENV = Path(
    os.getenv("DICOM_SANDBOX_PYTHON_ENV", REPO / ".pixi" / "envs" / "dicom-sandbox")
).expanduser()
IDLE_SECONDS = 2 * 3600
WORKSPACE_DIRS = ("volumes", "previews", "documents", "scripts", "out")

# The local client copies this process's environment into every command, which
# includes the model API keys. Any variable that looks like a secret is blanked.
SECRET_NAME_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
SECRET_NAME_PREFIXES = (
    "AZURE_",
    "OPENAI_",
    "GEMINI_",
    "ANTHROPIC_",
    "AWS_",
    "GOOGLE_",
    "AGENT_",
)


class ImageViewer(Capability):
    """Only the SDK's ``view_image`` tool.

    The SDK ``Filesystem`` capability also adds ``apply_patch``, a custom tool that
    only the Responses API accepts. The agent writes files with the shell instead.
    """

    type: Literal["image_viewer"] = "image_viewer"

    def tools(self) -> list[Tool]:
        if self.session is None:
            raise ValueError("ImageViewer capability is not bound to a SandboxSession")

        return [ViewImageTool(session=self.session, user=self.run_as)]


def capabilities() -> list[Capability]:
    return [Shell(), ImageViewer()]


def workspace_for(key: str) -> Path:
    """The workspace folder for one consult, created on first use."""
    workspace = WORKSPACES / key
    for folder_name in WORKSPACE_DIRS:
        (workspace / folder_name).mkdir(parents=True, exist_ok=True)

    # Touch the folder on every request: remove_idle_workspaces goes by mtime.
    os.utime(workspace)
    return workspace


def sandbox_environment() -> dict[str, str]:
    """Variables set for sandboxed commands: sandbox Python first, secrets blanked."""
    environment: dict[str, str] = {
        # The sandbox env comes first, so `python` is the one with SimpleITK and
        # friends, not the server's Python.
        "PATH": os.pathsep.join(
            [str(SANDBOX_ENV / "bin"), "/usr/local/bin", "/usr/bin", "/bin"]
        ),
        "MPLBACKEND": "Agg",  # no display; figures are saved as files
        "PYTHONNOUSERSITE": "1",  # ignore packages in the user's ~/.local
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    for variable_name in os.environ:
        upper_name = variable_name.upper()
        looks_secret = upper_name.startswith(SECRET_NAME_PREFIXES) or any(
            marker in upper_name for marker in SECRET_NAME_MARKERS
        )
        if looks_secret:
            environment[variable_name] = ""

    return environment


def run_config(key: str) -> SandboxRunConfig:
    """Sandbox settings for one run of the agent on consult ``key``."""
    # A manifest root other than the SDK default makes the local client use that
    # folder as-is and keep it after the run, which is what lets files persist
    # across the POSTs of one consult.
    manifest = Manifest(
        root=str(workspace_for(key)),
        environment=Environment(value=sandbox_environment()),  # type: ignore[arg-type]
    )
    return SandboxRunConfig(client=_client(), manifest=manifest)


def _client():  # type: ignore[no-untyped-def]
    sandbox_kind = os.getenv("DICOM_SANDBOX", "local").strip().lower()

    if sandbox_kind == "local":
        from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

        if not (SANDBOX_ENV / "bin" / "python").exists():
            raise RuntimeError(
                f"No sandbox Python at {SANDBOX_ENV}. Run: pixi install -e dicom-sandbox"
            )
        return UnixLocalSandboxClient()

    if sandbox_kind == "docker":
        raise NotImplementedError(
            "DICOM_SANDBOX=docker is not set up yet; use DICOM_SANDBOX=local."
        )

    raise ValueError(
        f"Unknown DICOM_SANDBOX={sandbox_kind!r}; expected local or docker"
    )


def remove_idle_workspaces(max_idle: float = IDLE_SECONDS) -> int:
    """Delete workspaces untouched for ``max_idle`` seconds. Returns how many."""
    if not WORKSPACES.exists():
        return 0

    removed_count = 0
    cutoff = time.time() - max_idle
    for workspace in WORKSPACES.iterdir():
        if workspace.is_dir() and workspace.stat().st_mtime < cutoff:
            shutil.rmtree(workspace, ignore_errors=True)
            removed_count += 1

    return removed_count
