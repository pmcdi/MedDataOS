"""Move base64 payloads from MedDataOS tool results into the sandbox workspace.

``fetch`` returns volumes as ``volume_base64`` (.nii.gz), slice previews as
``image_base64`` (PNG) and PDF pages as ``pdf_base64``. The model never sees these
strings: each is written to the workspace once and replaced by its path there.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from pathlib import Path
from typing import Any

# Payload key -> (workspace folder, file suffix)
PAYLOAD_DESTINATIONS = {
    "volume_base64": ("volumes", ".nii.gz"),
    "image_base64": ("previews", ".png"),
    "pdf_base64": ("documents", ".pdf"),
}

# Tool call ids become file names, so anything outside this set is replaced.
UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


def externalize(messages: list[dict], workspace: Path) -> list[dict]:
    """A copy of ``messages`` with tool-result payloads saved under ``workspace``."""
    updated_messages = []

    for message in messages:
        content = message.get("content")

        # Only tool results can carry payloads. A cheap substring check skips
        # parsing the JSON of every other tool result.
        is_tool_result = message.get("role") == "tool" and isinstance(content, str)
        if not is_tool_result or not any(
            key in content for key in PAYLOAD_DESTINATIONS
        ):
            updated_messages.append(message)
            continue

        try:
            parsed_content = json.loads(content)
        except json.JSONDecodeError:
            updated_messages.append(message)
            continue

        call_id = UNSAFE_FILENAME_CHARS.sub(
            "_", str(message.get("tool_call_id") or "call")
        )
        payload_count = [0]  # a list so the recursive calls share one counter
        rewritten_content = _replace_payloads(
            parsed_content, workspace, call_id, payload_count
        )

        updated_message = dict(message)
        updated_message["content"] = json.dumps(rewritten_content, ensure_ascii=False)
        updated_messages.append(updated_message)

    return updated_messages


def _replace_payloads(
    node: Any, workspace: Path, call_id: str, payload_count: list[int]
) -> Any:
    """Walk a parsed tool result and swap each payload for its workspace path."""
    if isinstance(node, list):
        return [
            _replace_payloads(item, workspace, call_id, payload_count) for item in node
        ]
    if not isinstance(node, dict):
        return node

    rewritten = {}
    for key, value in node.items():
        if key not in PAYLOAD_DESTINATIONS or not isinstance(value, str):
            rewritten[key] = _replace_payloads(value, workspace, call_id, payload_count)
            continue

        # The first payload of a call is named after the call; any further ones
        # get a suffix, so names stay the same each time MedDataOS re-sends them.
        folder, suffix = PAYLOAD_DESTINATIONS[key]
        payload_index = payload_count[0]
        payload_count[0] += 1
        file_stem = call_id if payload_index == 0 else f"{call_id}_{payload_index}"
        relative_path = f"{folder}/{file_stem}{suffix}"

        try:
            _write_once(workspace / relative_path, value)
            rewritten["workspace_path"] = relative_path
        except (binascii.Error, ValueError):
            rewritten[key] = "<undecodable base64 payload removed>"

    return rewritten


def _write_once(path: Path, encoded: str) -> None:
    """Decode and write ``encoded`` to ``path`` unless it is already there."""
    # MedDataOS re-sends the whole history on every POST, so the same payload
    # arrives many times; it is only decoded the first time.
    if path.exists() and path.stat().st_size > 0:
        return

    decoded = base64.b64decode(encoded, validate=True)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temporary name and rename, so a crash mid-write never leaves a
    # truncated file that the size check above would treat as complete.
    partial_path = path.with_suffix(path.suffix + ".part")
    partial_path.write_bytes(decoded)
    partial_path.replace(path)
