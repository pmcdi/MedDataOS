r"""``pixi run mcp``: register, inspect, remove and trust MCP servers.

    pixi run mcp add totalseg --scope project --evidence derived --router \\
        --sandbox --python-env .pixi/envs/totalseg \\
        --mount ~/.totalsegmentator/nnunet/results:/weights:ro \\
        --env TOTALSEG_WEIGHTS_PATH=/weights --timeout 1800 \\
        -- python servers/totalseg/server.py
    pixi run mcp add search --transport http --url http://127.0.0.1:9000/mcp
    pixi run mcp list
    pixi run mcp get totalseg
    pixi run mcp trust totalseg
    pixi run mcp remove search

Like ``claude mcp``: the scope is local (this checkout) unless ``--scope`` says
project (configs/mcp.json, committed) or user (~/.meddataos/mcp.json). A running
server reads the files at startup, so restart it after a change.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from meddataos.harness.mcp_config import (
    SERVER_EVIDENCE,
    McpConfigError,
    Scope,
    ServerConfig,
    load_servers,
    parse_entry,
    read_entries,
    scope_files,
    trust,
    trust_state,
    write_entries,
)


def build_entry(arguments: argparse.Namespace, command: list[str]) -> dict:
    """The ``mcpServers`` entry ``add`` writes: only what differs from defaults."""
    entry: dict = {}

    if arguments.transport == "http":
        entry = {"type": "http", "url": arguments.url}
    elif command:
        entry = {"command": command[0], "args": command[1:]}

    env = dict(item.split("=", 1) for item in arguments.env)

    if env:
        entry["env"] = env

    settings = {
        "evidence": arguments.evidence,
        "router": True if arguments.router else None,
        # 1800, not 1800.0, in the file a person reads.
        "timeout": (
            int(arguments.timeout)
            if arguments.timeout is not None and arguments.timeout.is_integer()
            else arguments.timeout
        ),
        "files": arguments.files,
        "sandbox": True if arguments.sandbox else None,
        "image": arguments.image,
        "python_env": arguments.python_env,
        "mounts": arguments.mount or None,
        "memory": arguments.memory,
        "cpus": arguments.cpus,
        "pin": False if arguments.no_pin else None,
    }
    block = {key: value for key, value in settings.items() if value is not None}

    if block:
        entry["meddataos"] = block

    return entry


def runs_as(server: ServerConfig) -> str:
    """Where a server runs, in a few words."""
    if server.url:
        return f"http {server.url}"

    if server.image:
        return f"image {server.image}"

    return "container" if server.sandbox else "host"


def cmd_add(arguments: argparse.Namespace, command: list[str]) -> int:
    """Register a server in one scope, checked before it is written."""
    scope = Scope(arguments.scope)
    path = scope_files()[scope]

    if arguments.transport == "http" and (command or not arguments.url):
        return fail("an http server takes --url and no command")

    if any("=" not in item for item in arguments.env):
        return fail("--env takes NAME=value")

    entries = read_entries(path)

    if arguments.name in entries:
        return fail(f"{arguments.name} is already in {path}; remove it first")

    entry = build_entry(arguments, command)
    # Checked exactly as the server will read it, before anything is written.
    server = parse_entry(arguments.name, entry, scope, path)
    entries[arguments.name] = entry
    write_entries(path, entries)
    print(f"Added {arguments.name} to {path} ({scope} scope), {runs_as(server)}.")

    if scope is Scope.PROJECT:
        print(
            "A project server starts once approved: "
            f"pixi run mcp trust {arguments.name}"
        )

    print("Restart the MedDataOS server to connect it.")
    return 0


def cmd_list(_: argparse.Namespace) -> int:
    """Print every registered server: scope, where it runs, evidence, trust."""
    servers = load_servers()

    if not servers:
        print("No MCP servers registered. Add one with: pixi run mcp add")
        return 0

    rows = [("NAME", "SCOPE", "RUNS", "EVIDENCE", "ROUTER", "TRUST")]
    rows += [
        (
            server.name,
            server.scope.value,
            runs_as(server),
            server.evidence.value,
            "yes" if server.router else "no",
            trust_state(server),
        )
        for server in servers
    ]
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]

    for row in rows:
        print(
            "  ".join(
                cell.ljust(width) for cell, width in zip(row, widths, strict=True)
            )
        )

    return 0


def cmd_get(arguments: argparse.Namespace) -> int:
    """Print one server as an approval covers it, env values hidden."""
    server = find(arguments.name)

    if server is None:
        return fail(f"no server named {arguments.name}")

    entry = read_entries(scope_files()[server.scope])[server.name]
    print(f"{server.name}  ({server.scope} scope, {server.source})")
    print(f"  runs        {runs_as(server)}")

    if server.command:
        print(f"  command     {' '.join(server.command)}")

    print(f"  evidence    {server.evidence}  (derived: what it returns can be cited)")
    print(f"  router      {'yes' if server.router else 'no'}")
    print(f"  timeout     {server.timeout:.0f} s")

    for spec in server.mounts:
        print(f"  mount       {spec}")

    # Values can be secrets; the keys say enough to review.
    for key, _ in server.env:
        print(f"  env         {key}")

    print(f"  trust       {trust_state(server)}")
    print()
    print(json.dumps({server.name: entry}, indent=2))
    return 0


def cmd_remove(arguments: argparse.Namespace) -> int:
    """Remove a server's registration from the one scope that holds it."""
    files = scope_files()
    scopes = [Scope(arguments.scope)] if arguments.scope else list(Scope)
    holding = [
        scope for scope in scopes if arguments.name in read_entries(files[scope])
    ]

    if not holding:
        return fail(f"no server named {arguments.name}")

    if len(holding) > 1:
        names = ", ".join(scope.value for scope in holding)
        return fail(f"{arguments.name} is in several scopes ({names}); pass --scope")

    path = files[holding[0]]
    entries = read_entries(path)
    del entries[arguments.name]
    write_entries(path, entries)
    print(f"Removed {arguments.name} from {path}.")
    return 0


def cmd_trust(arguments: argparse.Namespace) -> int:
    """Approve a server as registered now; its tools are pinned again."""
    server = find(arguments.name)

    if server is None:
        return fail(f"no server named {arguments.name}")

    trust(server)
    print(f"Trusted {server.name} as registered in {server.source}:")
    print(
        f"  {runs_as(server)}; evidence {server.evidence}; "
        f"router {'yes' if server.router else 'no'}"
    )
    print("Its tools are pinned again when it next connects. Restart the server.")
    return 0


def find(name: str) -> ServerConfig | None:
    """The registered server with this name, from the narrowest scope."""
    return next((server for server in load_servers() if server.name == name), None)


def fail(message: str) -> int:
    """Print why a command failed and return its exit code."""
    print(f"mcp: {message}", file=sys.stderr)
    return 2


def parser() -> argparse.ArgumentParser:
    """The ``mcp`` command line and its subcommands."""
    root = argparse.ArgumentParser(
        prog="pixi run mcp",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = root.add_subparsers(dest="action", required=True)

    add = commands.add_parser(
        "add", help="Register a server: add NAME [options] -- COMMAND"
    )
    add.add_argument("name")
    add.add_argument(
        "--scope", choices=[scope.value for scope in Scope], default="local"
    )
    add.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    add.add_argument("--url", help="The server's endpoint, with --transport http")
    add.add_argument("--env", action="append", default=[], metavar="NAME=value")
    add.add_argument(
        "--evidence", choices=[evidence.value for evidence in SERVER_EVIDENCE]
    )
    add.add_argument(
        "--router",
        action="store_true",
        help="Offer this server's tools to the main model",
    )
    add.add_argument(
        "--sandbox", action="store_true", help="Run the command in a container"
    )
    add.add_argument(
        "--image", help="A container image holding the server (runs sandboxed)"
    )
    add.add_argument("--python-env", help="A pixi environment to mount read-only")
    add.add_argument("--mount", action="append", metavar="HOST[:CONTAINER][:ro|rw]")
    add.add_argument("--timeout", type=float, help="Seconds per call (default 300)")
    add.add_argument("--memory")
    add.add_argument("--cpus")
    add.add_argument("--files", choices=["workspace", "none"])
    add.add_argument(
        "--no-pin", action="store_true", help="Do not pin its tool definitions"
    )

    commands.add_parser("list", help="Every registered server, its scope and trust")

    get = commands.add_parser(
        "get", help="One server's registration, as approval sees it"
    )
    get.add_argument("name")

    remove = commands.add_parser("remove", help="Remove a server's registration")
    remove.add_argument("name")
    remove.add_argument("--scope", choices=[scope.value for scope in Scope])

    trusted = commands.add_parser(
        "trust", help="Approve a server as registered now, and re-pin its tools"
    )
    trusted.add_argument("name")
    return root


def main(argv: Sequence[str] | None = None) -> int:
    """Run one ``mcp`` command; the server's command follows ``--``."""
    words = list(sys.argv[1:] if argv is None else argv)
    command: list[str] = []

    if "--" in words:
        split = words.index("--")
        words, command = words[:split], words[split + 1 :]

    arguments = parser().parse_args(words)

    try:
        if arguments.action == "add":
            return cmd_add(arguments, command)

        if command:
            return fail("only add takes a command after --")

        handlers = {
            "list": cmd_list,
            "get": cmd_get,
            "remove": cmd_remove,
            "trust": cmd_trust,
        }
        return handlers[arguments.action](arguments)
    except McpConfigError as exc:
        return fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
