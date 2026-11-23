"""The MCP hub: connects the registered servers and calls their tools.

Servers are registered in ``mcpServers`` JSON files (see ``mcp_config``) and
managed with ``pixi run mcp``. Every server's tools are offered as
``mcp__<server>__<tool>`` and registered with the server's evidence class. A
``derived`` server's calls are recorded under a cite token like an exec run, so
a number it returned can be cited and checked; the server's name and version
from the MCP handshake go with every result.

Isolation. A sandboxed server (``sandbox`` or ``image``) runs under
``docker run -i`` with the sandbox's runtime rule (gVisor unless
HARNESS_SANDBOX_RUNTIME names another), no network, no capabilities, a
read-only root, the session workspaces as the only writable mount, and its own
read-only mounts. An image is never pulled: it must be on the machine. When no
such container can start, the server is refused, never run on the host.

Trust. A project server must be approved here before it starts, and every
server's tool definitions are pinned; see ``mcp_config``.

Files travel as workspace paths, never as payloads. A parameter the server
marks as a path (``"format": "path"`` in its schema, or a name ending in
``_path`` or ``_dir``) is handed over as an absolute path inside this session's
workspace. A server that marks none gets the older rule: any string starting
with a workspace folder (``volumes/``, ``out/``, ...). The workspace prefix is
taken off what comes back.

A server that drops its connection is reconnected with backoff; a call that
finds the connection gone starts that. A sandboxed server's container is left
in place (no ``--rm``) until that happens, so a lost connection can be read
with ``docker inspect``: an OOM kill is reported as one, and the container is
then removed. The ``Client`` of each server lives on one background event loop
for the life of the process; calls from the harness's worker threads are sent
to that loop.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from meddataos.core import registry
from meddataos.core.workspace import REPO, WORKSPACES
from meddataos.harness.agents import expand_grants
from meddataos.harness.mcp_config import (
    ROUTER,
    ApprovalNeeded,
    PinMismatch,
    ServerConfig,
    check_approval,
    check_pins,
    fingerprints,
)
from meddataos.harness.sandbox import (
    SandboxSettings,
    isolation_flags,
    remove_container,
    runtime_problem,
)

CONNECT_TIMEOUT = 60.0
# Folders a workspace path argument may start with, for servers that mark none.
WORKSPACE_FOLDERS = ("volumes/", "previews/", "documents/", "out/", "scripts/")
MAX_IMAGE_BYTES = 4 * 1024 * 1024
# A description is prompt text; a server cannot fill the context with it.
MAX_DESCRIPTION_CHARS = 2000
# Reconnecting: waits double from 2 s up to a minute, for this many tries in a row.
MAX_RECONNECTS = 6
MAX_RECONNECT_WAIT = 60.0
# What a sandboxed server's container gets beyond its own settings.
CONTAINER_TMP = "--tmpfs=/tmp:rw,size=4g"
CONTAINER_SHM = "--shm-size=2g"
CONTAINER_PIDS = "--pids-limit=2048"
# The kernel's OOM killer delivers SIGKILL. Docker records that as exit 137,
# and sets State.OOMKilled when its own limit did the killing.
OOM_EXIT_CODE = 137


@dataclass
class McpTool:
    """One tool a server offers, under its harness name."""

    server: ServerConfig
    tool_name: str
    name: str
    definition: dict = field(default_factory=dict)
    # Parameters the server marks as paths; None when it marks none.
    path_params: frozenset[str] | None = None


class McpError(Exception):
    """A server could not be reached, or refused a call."""


class LaunchProblem(Exception):
    """A server cannot start here, and retrying will not change that."""


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------


def _host_path(text: str) -> Path:
    """A config path: ~ expanded, relative to the repository."""
    path = Path(text).expanduser()
    return path if path.is_absolute() else REPO / path


def resolve_command(command: Iterable[str]) -> list[str]:
    """The command with repository files made absolute, so it runs from anywhere."""
    resolved = []

    for part in command:
        candidate = REPO / part

        if not Path(part).is_absolute() and "/" in part and candidate.exists():
            resolved.append(str(candidate))
        else:
            resolved.append(part)

    return resolved


def parse_mount(spec: str) -> tuple[Path, str, str]:
    """``host[:container][:ro|rw]`` as (host path, container path, mode).

    The container path defaults to the host path; the mode to ro.
    """
    parts = spec.split(":")
    mode = "ro"

    if parts[-1] in ("ro", "rw"):
        mode = parts.pop()

    if not parts or len(parts) > 2 or not parts[0]:
        raise ValueError(f"mount {spec!r} is not host[:container][:ro|rw]")

    host = _host_path(parts[0])
    return host, parts[1] if len(parts) == 2 else str(host), mode


def container_command(
    server: ServerConfig, settings: SandboxSettings, container_name: str
) -> list[str]:
    """The ``docker run -i`` that starts a sandboxed server.

    Its only writable mount is the workspaces folder, at the same path, so the
    absolute workspace paths the hub hands over mean the same inside. A server
    with its own image keeps the image's PATH, working folder and entrypoint.
    """
    WORKSPACES.mkdir(parents=True, exist_ok=True)
    workspaces = WORKSPACES.resolve()
    arguments = [
        "docker",
        "run",
        "-i",
        f"--name={container_name}",
        *isolation_flags(settings),
        CONTAINER_TMP,
        CONTAINER_SHM,
        CONTAINER_PIDS,
        f"--memory={server.memory or settings.memory}",
        f"--cpus={server.cpus or settings.cpus}",
        f"--volume={workspaces}:{workspaces}:rw",
        "--env=HOME=/tmp",
        "--env=PYTHONDONTWRITEBYTECODE=1",
        "--env=MPLBACKEND=Agg",
    ]

    if server.image:
        # Never fetched behind the user's back: the image must already be here.
        arguments.append("--pull=never")
    else:
        arguments.append("--workdir=/tmp")

    path = "" if server.image else "/usr/local/bin:/usr/bin:/bin"

    if server.python_env:
        environment = _host_path(server.python_env).resolve()
        arguments.append(f"--volume={environment}:{environment}:ro")
        path = f"{environment}/bin:{path or '/usr/local/bin:/usr/bin:/bin'}"

    if path:
        arguments.append(f"--env=PATH={path}")

    command = resolve_command(server.command)

    # The server's own files, read-only where they are.
    for part in command:
        if part.startswith(str(REPO)) and Path(part).exists():
            folder = Path(part).parent.resolve()
            arguments.append(f"--volume={folder}:{folder}:ro")

    for spec in server.mounts:
        host, inside, mode = parse_mount(spec)
        arguments.append(f"--volume={host.resolve()}:{inside}:{mode}")

    for key, value in server.env:
        arguments.append(f"--env={key}={value}")

    return [*arguments, server.image or settings.image, *command]


def image_present(image: str) -> bool:
    """Whether Docker has this image locally."""
    try:
        found = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    return found.returncode == 0


def launch_problem(server: ServerConfig, settings: SandboxSettings) -> str | None:
    """Why a sandboxed server cannot start, or None. Checked before each start."""
    if not server.sandbox:
        return None

    problem = runtime_problem(settings)

    if problem:
        return f"refused to run outside a container: {problem}"

    if server.image and not image_present(server.image):
        return (
            f"image {server.image} is not on this machine; build or pull it "
            "(it is never pulled automatically)"
        )

    if server.python_env and not (_host_path(server.python_env) / "bin").is_dir():
        return f"no environment at {server.python_env}"

    for spec in server.mounts:
        try:
            host, _, _ = parse_mount(spec)
        except ValueError as exc:
            return str(exc)

        if not host.exists():
            return f"mount source {host} does not exist"

    return None


def container_state(name: str) -> dict | None:
    """``OOMKilled``, exit code and status for a container still on the host.

    The stdio EOF can arrive before dockerd has recorded the exit, so a
    container that is still ``running`` is polled for a moment.
    """
    seen = None

    for attempt in range(5):
        seen = _inspect_container(name)

        if seen is None or seen["Status"] in ("exited", "dead"):
            return seen

        if attempt < 4:
            time.sleep(0.2)

    return seen


def _inspect_container(name: str) -> dict | None:
    """One ``docker inspect`` of a container's state, or None when it is gone."""
    try:
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.OOMKilled}} {{.State.ExitCode}} {{.State.Status}}",
                name,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if inspected.returncode != 0:
        return None

    parts = inspected.stdout.split()

    if len(parts) != 3:
        return None

    try:
        exit_code = int(parts[1])
    except ValueError:
        return None

    return {
        "OOMKilled": parts[0].lower() == "true",
        "ExitCode": exit_code,
        "Status": parts[2],
    }


def memory_kill_message(limit: str) -> str:
    """What the model and the status line are told when a server is OOM-killed."""
    return (
        f"the server ran out of memory (limit {limit}); "
        "raise memory in its registration"
    )


# ---------------------------------------------------------------------------
# Workspace paths
# ---------------------------------------------------------------------------


def path_params_of(schema: dict | None) -> frozenset[str] | None:
    """Parameters a tool's schema marks as paths, or None when it marks none."""
    properties = (schema or {}).get("properties") or {}
    marked = {
        name
        for name, spec in properties.items()
        if isinstance(spec, dict)
        and (
            spec.get("format") == "path"
            or name == "path"
            or name.endswith(("_path", "_dir"))
        )
    }
    return frozenset(marked) or None


def _inside(value: str, workspace: Path) -> str:
    """One workspace path, absolute; refused when it leaves the workspace."""
    root = workspace.resolve()
    path = (root / value).resolve()

    # "out/../../etc" looks like a workspace path; the resolved path decides.
    if not path.is_relative_to(root):
        raise McpError(f"{value} is outside the workspace")

    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def resolve_marked(arguments: dict, marked: frozenset[str], workspace: Path) -> dict:
    """The marked parameters made absolute workspace paths; the rest untouched."""
    resolved = dict(arguments)

    for name in marked & set(arguments):
        value = arguments[name]

        if isinstance(value, str):
            resolved[name] = _inside(value, workspace)
        elif isinstance(value, list):
            resolved[name] = [
                _inside(item, workspace) if isinstance(item, str) else item
                for item in value
            ]

    return resolved


def resolve_paths(value: Any, workspace: Path) -> Any:
    """For servers that mark no paths: strings starting with a workspace folder."""
    if isinstance(value, dict):
        return {key: resolve_paths(item, workspace) for key, item in value.items()}

    if isinstance(value, list):
        return [resolve_paths(item, workspace) for item in value]

    if not isinstance(value, str) or not value.startswith(WORKSPACE_FOLDERS):
        return value

    return _inside(value, workspace)


def relativize(value: Any, workspace: Path) -> Any:
    """What a server returned, with this workspace's absolute prefix taken off."""
    prefix = str(workspace.resolve()) + "/"

    if isinstance(value, str):
        return value.replace(prefix, "")

    if isinstance(value, dict):
        return {key: relativize(item, workspace) for key, item in value.items()}

    if isinstance(value, list):
        return [relativize(item, workspace) for item in value]

    return value


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def convert_result(result: Any, workspace: Path | None) -> dict:
    """A CallToolResult as the harness's tool result.

    Text and structured content become ``text`` and ``structured``. The first
    image, when small enough, comes back under ``attach_image`` for the loop to
    show the model. An error result raises McpError with the server's words.
    """
    texts: list[str] = []
    image: dict | None = None

    for item in getattr(result, "content", None) or []:
        kind = getattr(item, "type", "")

        if kind == "text":
            texts.append(str(getattr(item, "text", "")))
        elif kind == "image" and image is None:
            data = str(getattr(item, "data", ""))

            if len(base64.b64decode(data or "", validate=False)) <= MAX_IMAGE_BYTES:
                image = {
                    "mime_type": str(getattr(item, "mime_type", "image/png")),
                    "image_base64": data,
                }

    text = "\n".join(part for part in texts if part)

    if getattr(result, "is_error", False):
        raise McpError(text or "the server reported an error")

    output: dict = {"text": text}
    structured = getattr(result, "structured_content", None)

    if structured:
        output["structured"] = structured

    if workspace is not None:
        output = relativize(output, workspace)

    if image is not None:
        output["attach_image"] = image

    return output


# ---------------------------------------------------------------------------
# The hub
# ---------------------------------------------------------------------------


class McpHub:
    """Every configured server, connected on one background event loop."""

    def __init__(
        self,
        servers: Iterable[ServerConfig],
        trust_store: Path | None = None,
        sandbox: SandboxSettings | None = None,
    ) -> None:
        """Servers to connect when ``start`` is called.

        ``trust_store`` holds approvals and pinned tools (default
        ``$HARNESS_CACHE/mcp_trust.json``);
        ``sandbox`` the container settings for sandboxed servers (default from
        the HARNESS_SANDBOX_* variables).
        """
        self.servers = {server.name: server for server in servers}
        self.status: dict[str, str] = {name: "not started" for name in self.servers}
        self.tools: dict[str, McpTool] = {}
        # Each server's name and version from its handshake, for provenance.
        self.server_info: dict[str, dict[str, str]] = {}
        # None means the default store, looked up when it is read.
        self.trust_store = trust_store
        self.sandbox = sandbox or SandboxSettings.from_env()
        self._clients: dict[str, Any] = {}
        self._broken: dict[str, asyncio.Event] = {}
        self._containers: dict[str, str] = {}
        # Kept so the tool error still explains an OOM kill after the container
        # has been removed.
        self._memory_kills: dict[str, str] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop: asyncio.Event | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self, timeout: float = CONNECT_TIMEOUT) -> None:
        """Connect every server; return once each is connected or has failed once."""
        if not self.servers or self._thread is not None:
            return

        loop = asyncio.new_event_loop()
        self._loop = loop
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._serve_all(ready))

        self._thread = threading.Thread(target=run, name="mcp", daemon=True)
        self._thread.start()

        if not ready.wait(timeout):
            for name, status in self.status.items():
                if status == "connecting":
                    self.status[name] = f"failed: no answer within {timeout:.0f} s"

    def stop(self) -> None:
        """Close every connection and the loop; remove any server container left."""
        loop, stop = self._loop, self._stop

        if loop is None or stop is None or self._thread is None:
            return

        loop.call_soon_threadsafe(stop.set)
        self._thread.join(timeout=15)
        self._thread = None

        # A server that ignored the closed stdin would keep its container.
        for container in list(self._containers.values()):
            remove_container(container)

    async def _serve_all(self, ready: threading.Event) -> None:
        self._stop = asyncio.Event()
        first_try = {name: asyncio.Event() for name in self.servers}
        tasks = [
            asyncio.create_task(self._serve(server, first_try[name]))
            for name, server in self.servers.items()
        ]

        await asyncio.gather(*(done.wait() for done in first_try.values()))
        ready.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _serve(self, server: ServerConfig, first_try: asyncio.Event) -> None:
        """Keep one server connected until the hub stops, reconnecting with backoff."""
        assert self._stop is not None
        failures = 0

        while not self._stop.is_set():
            try:
                await self._connect(server, first_try)
                failures = 0
            except Exception as exc:  # noqa: BLE001 - a server that fails is a status
                # The client's task group wraps what was raised inside it.
                cause = _root(exc)

                if isinstance(cause, LaunchProblem):
                    self.status[server.name] = f"failed: {cause}"
                    first_try.set()
                    return

                if isinstance(cause, ApprovalNeeded):
                    self.status[server.name] = f"awaiting approval: {cause}"
                    first_try.set()
                    return

                if isinstance(cause, PinMismatch):
                    self.status[server.name] = f"refused: {cause}"
                    first_try.set()
                    return

                # Same limit on the next start would die the same way.
                oom = self._memory_kill(server)

                if oom:
                    self.status[server.name] = f"failed: {oom}"
                    first_try.set()
                    return

                failures += 1
                reason = _reason(exc)

                if failures > MAX_RECONNECTS:
                    self.status[server.name] = f"failed: {reason} (gave up)"
                    first_try.set()
                    return

                wait = min(MAX_RECONNECT_WAIT, 2.0**failures)
                self.status[server.name] = f"failed: {reason}; retrying in {wait:.0f} s"
                first_try.set()
                await self._wait_or_stop(wait)
            finally:
                self._clients.pop(server.name, None)
                container = self._containers.pop(server.name, None)

                if container:
                    await asyncio.to_thread(remove_container, container)

    async def _connect(self, server: ServerConfig, first_try: asyncio.Event) -> None:
        """One connection: list and register tools, then hold it until stop or loss."""
        from mcp import Client, StdioServerParameters

        assert self._stop is not None
        self.status[server.name] = "connecting"
        # Not retried: an approval or a missing runtime does not come by itself.
        check_approval(server, self.trust_store)
        problem = launch_problem(server, self.sandbox)

        if problem:
            # Not retried: a missing runtime or mount does not fix itself.
            raise LaunchProblem(problem)

        target: Any = server.url or self._stdio_target(server, StdioServerParameters)
        broken = asyncio.Event()
        self._broken[server.name] = broken

        async with Client(target, read_timeout_seconds=server.timeout) as client:
            listed = (await client.list_tools()).tools

            if server.pin:
                check_pins(server.name, fingerprints(listed), self.trust_store)

            info = getattr(client, "server_info", None)
            self.server_info[server.name] = {
                "name": str(getattr(info, "name", "") or server.name),
                "version": str(getattr(info, "version", "") or ""),
            }
            self._register(server, listed)
            self._clients[server.name] = client
            count = sum(1 for tool in self.tools.values() if tool.server is server)
            self.status[server.name] = f"connected ({count} tools)"
            first_try.set()

            # Held until the hub stops or a call finds the connection gone. No
            # idle ping: MCP removed it (2026-07-28), and a failing ping would
            # tear down the connection under a long call in flight.
            await self._wait_for_any(self._stop, broken)

            if broken.is_set() and not self._stop.is_set():
                raise ConnectionError("the connection was lost")

    def _stdio_target(self, server: ServerConfig, parameters: Any) -> Any:
        command = resolve_command(server.command)

        if server.sandbox:
            container = f"meddataos-mcp-{server.name}-{uuid4().hex[:8]}"
            self._containers[server.name] = container
            command = container_command(server, self.sandbox, container)

        # The server's own environment rides on the docker command when it is
        # sandboxed; a bare server gets it beside the harness's environment.
        environment = dict(os.environ)

        if not server.sandbox:
            environment.update(dict(server.env))

        return parameters(command=command[0], args=command[1:], env=environment)

    async def _wait_for_any(self, first: asyncio.Event, second: asyncio.Event) -> None:
        """Wait until either event is set."""
        waiters = [asyncio.ensure_future(event.wait()) for event in (first, second)]
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)

        for waiter in waiters:
            waiter.cancel()

    async def _wait_or_stop(self, seconds: float) -> None:
        assert self._stop is not None

        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    def _register(self, server: ServerConfig, listed: Iterable[Any]) -> None:
        for tool in listed:
            name = f"mcp__{server.name}__{tool.name}"
            description = str(tool.description or tool.name)[:MAX_DESCRIPTION_CHARS]
            schema = tool.input_schema or {"type": "object", "properties": {}}

            if server.workspace_paths:
                description += (
                    " Paths are workspace paths such as volumes/<file>.nii.gz; "
                    "write outputs under out/."
                )

            self.tools[name] = McpTool(
                server=server,
                tool_name=str(tool.name),
                name=name,
                definition={
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": f"[{server.name}] {description}",
                        "parameters": schema,
                    },
                },
                path_params=path_params_of(schema),
            )
            registry.register(name, server.evidence, f"mcp:{server.name}")

    # -- grants -------------------------------------------------------------

    def names_for(self, who: str | set[str]) -> set[str]:
        """Tools for the router, or the granted names that are connected.

        ``"router"`` is the main model: servers with ``router`` set. A set of
        grants expands ``mcp__<server>__*`` to that server's connected tools
        and nothing else. A plain name that is not an MCP tool is dropped.
        """
        if who == ROUTER:
            return {tool.name for tool in self.tools.values() if tool.server.router}

        grants = who if isinstance(who, set) else {who}
        expanded = expand_grants(grants, (tool.name for tool in self.tools.values()))
        return {name for name in expanded if name in self.tools}

    def definitions_for(self, who: str | set[str]) -> list[dict]:
        """The function tools for the router, or for a set of grants."""
        allowed = self.names_for(who)
        return [tool.definition for tool in self.tools.values() if tool.name in allowed]

    # -- calls --------------------------------------------------------------

    def call(self, name: str, arguments: dict, workspace: Path | None) -> dict:
        """Call one tool and wait for it, within its server's timeout."""
        tool = self.tools.get(name)

        if tool is None:
            raise McpError(f"unknown MCP tool {name}")

        server = tool.server
        client = self._clients.get(server.name)

        if client is None or self._loop is None:
            raise McpError(
                f"MCP server {server.name} is not connected "
                f"({self.status.get(server.name)})"
            )

        path_root = workspace if server.workspace_paths else None

        if path_root is not None:
            arguments = (
                resolve_marked(arguments, tool.path_params, path_root)
                if tool.path_params is not None
                else resolve_paths(arguments, path_root)
            )

        future = asyncio.run_coroutine_threadsafe(
            client.call_tool(
                tool.tool_name, arguments, read_timeout_seconds=server.timeout
            ),
            self._loop,
        )

        try:
            # A little over the server's own timeout, so its error arrives first.
            result = future.result(timeout=server.timeout + 5)
        except TimeoutError as exc:
            future.cancel()
            raise McpError(
                f"{name} ran past {server.timeout:.0f} s and was stopped"
            ) from exc
        except Exception as exc:
            # A tool's own failure comes back as a result, not an exception, so
            # this is the connection: have it rebuilt. Inspect before that,
            # while the exited container is still there to be read.
            oom = self._memory_kill(server)
            self._mark_broken(server.name)

            if oom:
                self.status[server.name] = f"failed: {oom}"

            raise McpError(f"{name} failed: {oom or _reason(exc)}") from exc

        output = convert_result(result, path_root)
        output["server"] = self.server_info.get(server.name, {"name": server.name})
        return output

    def _mark_broken(self, server_name: str) -> None:
        """Retire a dead connection now, in the calling thread, then rebuild it.

        Done here and not only on the loop: until the loop runs, a second call
        would still find the dead client and the status would still say
        connected.
        """
        self._clients.pop(server_name, None)
        self.status[server_name] = "reconnecting: the connection was lost"
        broken = self._broken.get(server_name)

        if broken is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(broken.set)

    def _memory_kill(self, server: ServerConfig) -> str | None:
        """The out-of-memory line when this server's container was killed, else None.

        Remembered on the hub: the connection handler removes the container as
        soon as it sees the loss, and the tool call may still be raising.
        """
        remembered = self._memory_kills.get(server.name)

        if remembered:
            return remembered

        if not server.sandbox:
            return None

        name = self._containers.get(server.name)

        if not name:
            return None

        state = container_state(name)

        if not state:
            return None

        if not state["OOMKilled"] and state["ExitCode"] != OOM_EXIT_CODE:
            return None

        message = memory_kill_message(server.memory or self.sandbox.memory)
        self._memory_kills[server.name] = message
        return message


def _root(exc: BaseException) -> BaseException:
    """The first exception inside any task-group wrapping."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]

    return exc


def _reason(exc: BaseException) -> str:
    """One readable line for an error, unwrapping task groups."""
    cause = _root(exc)
    return str(cause) or cause.__class__.__name__


def summary(output: dict) -> str:
    """The line a tool's result leads with, for the trace."""
    structured = output.get("structured")

    if isinstance(structured, dict) and isinstance(structured.get("summary"), str):
        return str(structured["summary"])

    text = str(output.get("text") or "")

    if not text and structured:
        text = json.dumps(structured)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0] if lines else ""
