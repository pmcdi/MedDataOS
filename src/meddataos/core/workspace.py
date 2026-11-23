"""Keep large binary payloads out of the model's context.

``fetch`` returns slice previews as ``image_base64`` (PNG) and PDF pages as
``pdf_base64``. A model cannot use a base64 string, and one payload can be
megabytes of context. Each is written to the session's workspace once and
replaced by its path there.

Volumes skip the round trip: in harness mode the server has datactrl write them
into the workspace's ``volumes/`` itself (``volume_path``), and only the path is
made relative here. ``volume_base64`` is still handled, for a result that
carries one.

"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
CACHE_DIR = Path(os.getenv("HARNESS_CACHE", REPO / ".cache" / "harness")).expanduser()
WORKSPACES = CACHE_DIR / "workspaces"

# Payload key -> (workspace folder, file suffix)
PAYLOAD_DESTINATIONS = {
    "volume_base64": ("volumes", ".nii.gz"),
    "image_base64": ("previews", ".png"),
    "pdf_base64": ("documents", ".pdf"),
}


def workspace_for(session_id: str) -> Path:
    """The workspace folder for one session, created on first use."""
    # Session ids come from the server, but a path part is never trusted as-is.
    safe_name = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in session_id
    )
    workspace = WORKSPACES / (safe_name or "session")
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def offload_payloads(result: Any, workspace: Path) -> Any:
    """A copy of a tool result, each base64 payload saved and swapped for its path."""
    if isinstance(result, list):
        return [offload_payloads(item, workspace) for item in result]

    if not isinstance(result, dict):
        return result

    rewritten: dict = {}

    for key, value in result.items():
        # Written straight to the workspace by datactrl: only the path changes.
        if key == "volume_path" and isinstance(value, str):
            rewritten.update(_workspace_file(Path(value), workspace))
            continue

        if key not in PAYLOAD_DESTINATIONS or not isinstance(value, str):
            rewritten[key] = offload_payloads(value, workspace)
            continue

        folder, suffix = PAYLOAD_DESTINATIONS[key]

        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            rewritten[key] = "<undecodable base64 payload removed>"
            continue

        # Named by content, so the same payload fetched twice is one file.
        digest = hashlib.sha256(decoded).hexdigest()[:16]
        path = workspace / folder / f"{digest}{suffix}"
        _write_once(path, decoded)
        rewritten["workspace_path"] = str(path.relative_to(workspace))
        rewritten["workspace_bytes"] = len(decoded)

    return rewritten


def _workspace_file(path: Path, workspace: Path) -> dict:
    """A file already in the workspace, as the model sees it: a relative path."""
    root = workspace.resolve()
    resolved = path.resolve()

    # A path outside this session's workspace is not shown to the model.
    if not resolved.is_relative_to(root) or not resolved.is_file():
        return {"workspace_path": "<file outside the workspace removed>"}

    return {
        "workspace_path": str(resolved.relative_to(root)),
        "workspace_bytes": resolved.stat().st_size,
    }


def _write_once(path: Path, data: bytes) -> None:
    if path.exists() and path.stat().st_size == len(data):
        return

    path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temporary name and rename, so a crash mid-write never leaves a
    # truncated file that the size check above would treat as complete.
    partial_path = path.with_suffix(path.suffix + ".part")
    partial_path.write_bytes(data)
    partial_path.replace(path)
