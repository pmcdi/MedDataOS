"""The harness config file, ``configs/harness.toml``.

It holds the ``[agent.<name>]`` tables: local overrides for an agent file in
``agents/``. MCP servers are registered in their own files; see
``meddataos.harness.mcp_config``.
``HARNESS_CONFIG`` points at another file, which tests use so a developer's own
config never leaks into them.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from meddataos.core.workspace import REPO

DEFAULT_CONFIG = REPO / "configs" / "harness.toml"


def config_path() -> Path:
    """The harness config in use: HARNESS_CONFIG, else configs/harness.toml."""
    return Path(os.getenv("HARNESS_CONFIG") or DEFAULT_CONFIG).expanduser()


def tables(section: str, path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The ``[<section>.<name>]`` tables of the config, by name; nothing if absent."""
    source = path or config_path()

    if not source.is_file():
        return {}

    data = tomllib.loads(source.read_text())
    found = data.get(section) or {}
    return {name: body for name, body in found.items() if isinstance(body, dict)}
