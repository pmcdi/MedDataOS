"""Where MCP servers are registered, and which of them are trusted.

Servers are registered in JSON files in Claude Code's ``mcpServers`` format, in
three scopes, later ones winning on a name:

    user     ~/.meddataos/mcp.json        yours, on every case (HARNESS_MCP_USER)
    project  configs/mcp.json             committed, shared (HARNESS_MCP_PROJECT)
    local    configs/mcp.local.json       this checkout only, not committed
                                          (HARNESS_MCP_LOCAL)

The file is not named ``.mcp.json`` on purpose: Claude Code reads that name,
and would offer a patient-data server to whoever codes in this repository.

    {
      "mcpServers": {
        "totalseg": {
          "command": "python",
          "args": ["servers/totalseg/server.py"],
          "env": {"TOTALSEG_WEIGHTS_PATH": "/weights"},
          "meddataos": {
            "evidence": "derived",
            "router": false,
            "sandbox": true,
            "python_env": ".pixi/envs/totalseg",
            "mounts": ["~/.totalsegmentator/nnunet/results:/weights:ro"],
            "timeout": 1800
          }
        },
        "search": {"type": "http", "url": "http://127.0.0.1:9000/mcp"}
      }
    }

``command``, ``args``, ``env``, ``type`` and ``url`` mean what they mean to
Claude Code (``headers`` is not supported yet), and ``${VAR}`` or
``${VAR:-default}`` in them is filled from the environment. Everything
MedDataOS adds is under ``meddataos``; see ``_meddataos_settings`` for the keys.

Trust. A server from the project file, which someone else may have written,
does not start until it is approved here with ``pixi run mcp trust <name>``,
and an edit to its entry (granting ``derived`` evidence, dropping the sandbox)
needs approval again. Every server's tool definitions are pinned on first
connect, and a change refuses the server until it is trusted again: a tool
description is text the model follows. Both live in
``$HARNESS_CACHE/mcp_trust.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from meddataos.core import config
from meddataos.core.registry import Evidence
from meddataos.core.workspace import CACHE_DIR, REPO

DEFAULT_TIMEOUT = 300.0
ROUTER = "router"
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
# Evidence a server may claim. Case evidence needs datactrl-style recording.
SERVER_EVIDENCE = (Evidence.DERIVED, Evidence.BACKGROUND, Evidence.NAVIGATION)
TRUST_STORE = CACHE_DIR / "mcp_trust.json"
ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class Scope(StrEnum):
    """Where a server is registered, from the widest to the narrowest."""

    USER = "user"
    PROJECT = "project"
    LOCAL = "local"


@dataclass(frozen=True)
class ServerConfig:
    """One MCP server: how to reach it, and how far its output is trusted."""

    name: str
    command: tuple[str, ...] = ()
    url: str = ""
    env: tuple[tuple[str, str], ...] = ()
    evidence: Evidence = Evidence.BACKGROUND
    router: bool = False
    timeout: float = DEFAULT_TIMEOUT
    workspace_paths: bool = True
    sandbox: bool = False
    image: str = ""
    python_env: str = ""
    mounts: tuple[str, ...] = ()
    memory: str = ""
    cpus: str = ""
    pin: bool = True
    scope: Scope = Scope.LOCAL
    source: str = ""


class McpConfigError(ValueError):
    """A registration file or entry is not valid."""


class PinMismatch(Exception):
    """A server's tools differ from the ones that were trusted."""


class ApprovalNeeded(Exception):
    """A project server has not been approved here, or changed since it was."""


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def scope_files() -> dict[Scope, Path]:
    """The registration file of each scope."""
    return {
        Scope.USER: Path(
            os.getenv("HARNESS_MCP_USER") or "~/.meddataos/mcp.json"
        ).expanduser(),
        Scope.PROJECT: Path(
            os.getenv("HARNESS_MCP_PROJECT") or REPO / "configs" / "mcp.json"
        ).expanduser(),
        Scope.LOCAL: Path(
            os.getenv("HARNESS_MCP_LOCAL") or REPO / "configs" / "mcp.local.json"
        ).expanduser(),
    }


def read_entries(path: Path) -> dict[str, dict]:
    """The ``mcpServers`` object of one file; nothing when the file is absent."""
    if not path.is_file():
        return {}

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"{path}: not valid JSON ({exc})") from exc

    servers = data.get("mcpServers", {}) if isinstance(data, dict) else None

    if not isinstance(servers, dict):
        raise McpConfigError(f"{path}: mcpServers must be an object")

    return servers


def write_entries(path: Path, entries: dict[str, dict]) -> None:
    """Rewrite one file's ``mcpServers``, keeping any other keys in it."""
    data: dict[str, Any] = {}

    if path.is_file():
        loaded = json.loads(path.read_text())
        data = loaded if isinstance(loaded, dict) else {}

    data["mcpServers"] = entries
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def load_servers() -> list[ServerConfig]:
    """Every registered server, the narrowest scope winning on a name.

    A bad file or entry raises McpConfigError, naming the file and the entry.
    """
    # MCP tables used to live in harness.toml; one left there would be ignored.
    if config.tables("mcp"):
        raise McpConfigError(
            f"{config.config_path()}: [mcp.*] tables moved to configs/mcp.json; "
            "register servers with: pixi run mcp add"
        )

    servers: dict[str, ServerConfig] = {}

    for scope, path in scope_files().items():
        for name, entry in read_entries(path).items():
            servers[name] = parse_entry(name, entry, scope, path)

    return list(servers.values())


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


def expand(value: str) -> str:
    """``${VAR}`` and ``${VAR:-default}`` filled from the environment."""
    return ENV_REFERENCE.sub(
        lambda match: os.getenv(match.group(1)) or (match.group(2) or ""), value
    )


def parse_entry(
    name: str, entry: Any, scope: Scope = Scope.LOCAL, path: Path | None = None
) -> ServerConfig:
    """One ``mcpServers`` entry as a ServerConfig, or McpConfigError saying why."""
    where = f"{path or 'mcpServers'}: {name}"

    def problem(message: str) -> McpConfigError:
        return McpConfigError(f"{where}: {message}")

    def strings(value: Any, key: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise problem(f"{key} must be a list of strings")

        return tuple(value)

    def pairs(value: Any, key: str) -> tuple[tuple[str, str], ...]:
        if not isinstance(value, dict):
            raise problem(f"{key} must be an object of NAME: value")

        return tuple((str(item), expand(str(value[item]))) for item in value)

    if not NAME.fullmatch(name):
        raise problem("the name must be lowercase letters, digits, _ or -")

    if not isinstance(entry, dict):
        raise problem("an entry must be an object")

    if "headers" in entry:
        raise problem("headers are not supported yet")

    transport = str(entry.get("type", "stdio"))

    if transport not in ("stdio", "http"):
        raise problem(f"type {transport!r} is not supported; use stdio or http")

    settings = _meddataos_settings(entry.get("meddataos", {}), problem, strings)
    command: tuple[str, ...] = ()

    if transport == "stdio":
        if "command" in entry and not isinstance(entry["command"], str):
            raise problem("command must be a string, its arguments in args")

        args = strings(entry.get("args", []), "args")
        command = tuple(
            expand(part) for part in (entry.get("command") or "", *args) if part
        )

        if not command and not settings["image"]:
            raise problem("a stdio server needs a command, or an image to run")
    elif not entry.get("url"):
        raise problem("an http server needs a url")

    if transport == "http" and (settings["sandbox"] or settings["image"]):
        raise problem("sandbox and image run a command; an http server runs elsewhere")

    return ServerConfig(
        name=name,
        command=command,
        url=expand(str(entry.get("url", ""))),
        env=pairs(entry.get("env", {}), "env"),
        scope=scope,
        source=str(path or ""),
        **settings,
    )


def _meddataos_settings(block: Any, problem: Any, strings: Any) -> dict[str, Any]:
    """The ``meddataos`` block: how far the server is trusted and how it runs.

    evidence    derived | background (default) | navigation
    router      offer this server's tools to the main model (default false).
                Agents grant themselves, in agents/<name>.md.
    timeout     seconds per call (default 300)
    files       "workspace" (path parameters become workspace paths) or "none"
    sandbox     run the command in a container (default false; true with image)
    image       a container image holding the server; it runs sandboxed
    python_env  a pixi environment mounted read-only, first on PATH
    mounts      more mounts, "host[:container][:ro|rw]", read-only by default
    memory, cpus  container limits (default the sandbox's)
    pin         refuse changed tool definitions until trusted (default true)
    """
    if not isinstance(block, dict):
        raise problem("meddataos must be an object")

    if "skills" in block:
        raise problem(
            "meddataos.skills is no longer read. Grants moved to the tools line "
            f'in agents/<name>.md; set "{ROUTER}": true to offer this server\'s '
            "tools to the main model."
        )

    known = {
        "evidence",
        "router",
        "timeout",
        "files",
        "sandbox",
        "image",
        "python_env",
        "mounts",
        "memory",
        "cpus",
        "pin",
    }
    unknown = set(block) - known

    if unknown:
        raise problem(f"unknown meddataos keys: {', '.join(sorted(unknown))}")

    try:
        evidence = Evidence(str(block.get("evidence", "background")))
    except ValueError as exc:
        raise problem(f"unknown evidence {block.get('evidence')!r}") from exc

    if evidence not in SERVER_EVIDENCE:
        allowed = ", ".join(item.value for item in SERVER_EVIDENCE)
        raise problem(f"evidence {evidence.value!r} is not offered yet; use {allowed}")

    files = str(block.get("files", "workspace"))
    if files not in ("workspace", "none"):
        raise problem("files must be workspace or none")

    image = str(block.get("image", ""))
    sandbox = bool(block.get("sandbox", bool(image)))

    if image and not sandbox:
        raise problem("an image always runs sandboxed; drop sandbox = false")

    router = block.get("router", False)

    if not isinstance(router, bool):
        raise problem("router must be true or false")

    return {
        "evidence": evidence,
        "router": router,
        "timeout": float(block.get("timeout", DEFAULT_TIMEOUT)),
        "workspace_paths": files == "workspace",
        "sandbox": sandbox,
        "image": image,
        "python_env": str(block.get("python_env", "")),
        "mounts": strings(block.get("mounts", []), "mounts"),
        "memory": str(block.get("memory", "")),
        "cpus": str(block.get("cpus", "")),
        "pin": bool(block.get("pin", True)),
    }


def config_hash(server: ServerConfig) -> str:
    """A hash of everything about a server an approval covers."""
    fields = asdict(server)
    fields.pop("source")
    encoded = json.dumps(fields, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# Trust
# ---------------------------------------------------------------------------


def _store(path: Path | None) -> Path:
    """The trust store to use: the one given, else TRUST_STORE, read at call time."""
    return path or TRUST_STORE


def _read_store(path: Path | None) -> dict[str, dict[str, Any]]:
    path = _store(path)

    try:
        store = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}

    return store if isinstance(store, dict) else {}


def _write_store(path: Path | None, store: dict[str, dict[str, Any]]) -> None:
    path = _store(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n")


def check_approval(server: ServerConfig, path: Path | None = None) -> None:
    """Raise ApprovalNeeded for a project server not approved as it stands.

    Servers you registered yourself (user and local scope) need no approval.
    """
    if server.scope is not Scope.PROJECT:
        return

    approved = _read_store(path).get(server.name, {}).get("approved")

    if approved == config_hash(server):
        return

    why = "changed since it was approved" if approved else "not approved here"
    raise ApprovalNeeded(
        f"it comes from {server.source} and is {why}. Review it with "
        f"`pixi run mcp get {server.name}`, then run: pixi run mcp trust {server.name}"
    )


def fingerprints(listed: Iterable[Any]) -> dict[str, str]:
    """A hash of each tool's description and schema, by tool name."""
    found = {}

    for tool in listed:
        definition = {
            "description": str(tool.description or ""),
            "schema": tool.input_schema or {},
        }
        encoded = json.dumps(definition, sort_keys=True).encode()
        found[str(tool.name)] = hashlib.sha256(encoded).hexdigest()

    return found


def check_pins(name: str, found: dict[str, str], path: Path | None = None) -> None:
    """Pin a server's tools on first sight; raise PinMismatch when they changed."""
    store = _read_store(path)
    record = store.setdefault(name, {})
    trusted = record.get("tools")

    if trusted is None:
        record["tools"] = found
        _write_store(path, store)
        return

    if trusted == found:
        return

    changed = [
        tool for tool in found if tool in trusted and found[tool] != trusted[tool]
    ]
    added = [tool for tool in found if tool not in trusted]
    removed = [tool for tool in trusted if tool not in found]
    changes = [
        f"{label}: {', '.join(sorted(names))}"
        for label, names in (
            ("changed", changed),
            ("added", added),
            ("removed", removed),
        )
        if names
    ]

    raise PinMismatch(
        f"its tools differ from the trusted ones ({'; '.join(changes)}). Review "
        f"the server, then run: pixi run mcp trust {name}"
    )


def trust(server: ServerConfig, path: Path | None = None) -> None:
    """Approve a server as registered now, and re-pin its tools on next connect."""
    store = _read_store(path)
    store[server.name] = {"approved": config_hash(server)}
    _write_store(path, store)


def trust_state(server: ServerConfig, path: Path | None = None) -> str:
    """How far a server is trusted, in a few words, for ``mcp list``."""
    record = _read_store(path).get(server.name, {})

    try:
        check_approval(server, path)
    except ApprovalNeeded:
        return "needs approval"

    if record.get("tools"):
        return f"approved, {len(record['tools'])} tools pinned"

    return "approved" if server.scope is Scope.PROJECT else "yours"
