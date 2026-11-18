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

# Environment variables that must not reach sandboxed commands.
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
_SECRET_PREFIXES = (
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
    path = WORKSPACES / key
    for name in WORKSPACE_DIRS:
        (path / name).mkdir(parents=True, exist_ok=True)
    os.utime(path)
    return path


def sandbox_environment() -> dict[str, str]:
    """Variables set for sandboxed commands: sandbox Python first, secrets blanked."""
    env: dict[str, str] = {
        "PATH": os.pathsep.join(
            [str(SANDBOX_ENV / "bin"), "/usr/local/bin", "/usr/bin", "/bin"]
        ),
        "MPLBACKEND": "Agg",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in os.environ:
        upper = name.upper()
        if upper.startswith(_SECRET_PREFIXES) or any(
            m in upper for m in _SECRET_MARKERS
        ):
            env[name] = ""
    return env


def run_config(key: str) -> SandboxRunConfig:
    """Sandbox settings for one run of the agent on consult ``key``."""
    workspace = workspace_for(key)
    manifest = Manifest(
        root=str(workspace),
        environment=Environment(value=sandbox_environment()),  # type: ignore[arg-type]
    )
    return SandboxRunConfig(client=_client(), manifest=manifest)


def _client():  # type: ignore[no-untyped-def]
    kind = os.getenv("DICOM_SANDBOX", "local").strip().lower()
    if kind == "local":
        from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

        if not (SANDBOX_ENV / "bin" / "python").exists():
            raise RuntimeError(
                f"No sandbox Python at {SANDBOX_ENV}. Run: pixi install -e dicom-sandbox"
            )
        return UnixLocalSandboxClient()
    if kind == "docker":
        raise NotImplementedError(
            "DICOM_SANDBOX=docker is not set up yet; use DICOM_SANDBOX=local."
        )
    raise ValueError(f"Unknown DICOM_SANDBOX={kind!r}; expected local or docker")


def remove_idle_workspaces(max_idle: float = IDLE_SECONDS) -> int:
    """Delete workspaces untouched for ``max_idle`` seconds. Returns how many."""
    if not WORKSPACES.exists():
        return 0
    removed = 0
    cutoff = time.time() - max_idle
    for path in WORKSPACES.iterdir():
        if path.is_dir() and path.stat().st_mtime < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed
