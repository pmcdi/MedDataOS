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

# key -> (workspace folder, file suffix)
_PAYLOADS = {
    "volume_base64": ("volumes", ".nii.gz"),
    "image_base64": ("previews", ".png"),
    "pdf_base64": ("documents", ".pdf"),
}
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def externalize(messages: list[dict], workspace: Path) -> list[dict]:
    """A copy of ``messages`` with tool-result payloads saved under ``workspace``."""
    out = []
    for message in messages:
        if message.get("role") != "tool" or not isinstance(message.get("content"), str):
            out.append(message)
            continue
        content = message["content"]
        if not any(key in content for key in _PAYLOADS):
            out.append(message)
            continue
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            out.append(message)
            continue
        call_id = _UNSAFE.sub("_", str(message.get("tool_call_id") or "call"))
        counter = [0]
        replaced = _walk(parsed, workspace, call_id, counter)
        updated = dict(message)
        updated["content"] = json.dumps(replaced, ensure_ascii=False)
        out.append(updated)
    return out


def _walk(node: Any, workspace: Path, call_id: str, counter: list[int]) -> Any:
    if isinstance(node, list):
        return [_walk(item, workspace, call_id, counter) for item in node]
    if not isinstance(node, dict):
        return node

    result = {}
    for key, value in node.items():
        if key in _PAYLOADS and isinstance(value, str):
            folder, suffix = _PAYLOADS[key]
            index = counter[0]
            counter[0] += 1
            name = call_id if index == 0 else f"{call_id}_{index}"
            relative = f"{folder}/{name}{suffix}"
            try:
                _write_once(workspace / relative, value)
                result["workspace_path"] = relative
            except (binascii.Error, ValueError):
                result[key] = "<undecodable base64 payload removed>"
            continue
        result[key] = _walk(value, workspace, call_id, counter)
    return result


def _write_once(path: Path, encoded: str) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    data = base64.b64decode(encoded, validate=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)
