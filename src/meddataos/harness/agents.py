"""Agents: a subagent's prompt, the skills it carries, and the tools it is granted.

A skill is know-how only (``skills/<name>/SKILL.md``). An agent is a file in
``agents/<name>.md``: who it is, which skills it preloads, and the only tool
grant, including MCP tools. ``configs/harness.toml`` may override the model or
the step budget for one deployment; it does not define the agent.

    ---
    name: dicom
    description: Scans and series - metadata, measurements, anatomy segmentation
    skills: dicom
    tools: dicom_tree, fetch, exec, mcp__totalseg__*
    max_steps: 24
    ---

The system prompt is the subagent rules, then this file's body, then each
listed skill's body, in order. ``tools`` is the grant: a plain name, an exact
``mcp__<server>__<tool>``, or ``mcp__<server>__*``. A pattern expands to that
server's tools once it has connected. A grant is a permission, not a
dependency: a server that is not registered here is warned about once and
expands to nothing, the same as a registered server that has not connected.
A granted tool that is not available (the sandbox is off, the server is not
connected) is simply not offered.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from meddataos.core import config, registry
from meddataos.core.model import (
    ModelClient,
    ModelSettings,
    client_from_settings,
    settings_from_env,
)
from meddataos.core.workspace import REPO
from meddataos.harness.frontmatter import front_matter
from meddataos.harness.skills import Skill

AGENTS_DIR = REPO / "agents"

DEFAULT_MAX_STEPS = 16
# What an agent may change about the model it talks to.
MODEL_KEYS = {
    "model_api": "api",
    "model": "model",
    "reasoning_effort": "reasoning_effort",
}
# What the router's roster names, besides a connected MCP server. Tools every
# agent has (describe, fetch) stay off this list.
NOTABLE_TOOLS = ("exec", "view_image")
# mcp__<server>__<tool> or mcp__<server>__*. The server name matches a registration.
MCP_GRANT = re.compile(r"^mcp__([a-z0-9][a-z0-9_-]{0,31})__(.+)$")


@dataclass(frozen=True)
class Agent:
    """One subagent: its prompt, the skills it preloads, and its tool grant."""

    name: str
    description: str
    body: str
    skills: tuple[str, ...]
    tools: tuple[str, ...]
    max_steps: int = DEFAULT_MAX_STEPS
    model_overrides: tuple[tuple[str, str], ...] = ()

    def client(self) -> ModelClient:
        """The model this agent talks to: the harness model with its overrides."""
        base = settings_from_env()
        overrides = dict(self.model_overrides)
        settings: ModelSettings = replace(
            base,
            api=overrides.get("api", base.api),
            model=overrides.get("model", base.model),
            reasoning_effort=overrides.get("reasoning_effort", base.reasoning_effort),
        )
        return client_from_settings(settings)

    def system_prompt(self, rules: str, skills: dict[str, Skill]) -> str:
        """Subagent rules, then this file's body, then each skill's body, in order."""
        parts = [rules]

        if self.body:
            parts.append(self.body)

        parts.extend(skills[name].body for name in self.skills)
        return "\n\n".join(parts)


def load_config(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The ``[agent.<name>]`` tables, after refusing a leftover ``[subagent.*]``."""
    refuse_subagent_tables(path)
    return config.tables("agent", path)


def refuse_subagent_tables(path: Path | None = None) -> None:
    """Stop when ``[subagent.*]`` is still in the harness config.

    Those tables moved to ``[agent.<name>]`` and to files in ``agents/``.
    """
    tables = config.tables("subagent", path)

    if not tables:
        return

    where = path or config.config_path()
    listed = ", ".join(f"[subagent.{name}]" for name in sorted(tables))
    raise SystemExit(
        f"{where}: {listed} moved to [agent.<name>] and agents/. "
        "A subagent is an agent file now; the config only overrides it."
    )


def load_agents(
    agents_dir: Path,
    skills: dict[str, Skill],
    registered_mcp_servers: Iterable[str],
    config_tables: dict[str, dict[str, Any]] | None = None,
    config_path: Path | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> dict[str, Agent]:
    """Agents from ``agents_dir``, with local overrides applied.

    ``registered_mcp_servers`` is the names in the MCP registration, not the
    servers that have connected: they connect later. A grant naming one that
    is not registered is kept, and reported through ``on_warning``: its tools
    are not offered. Raises ``SystemExit``, naming the file, when an agent
    names an unknown skill, grants an unknown plain tool or ``spawn_subagent``,
    or leaves a skill's ``requires`` uncovered. A ``[agent.<name>]`` table
    with no agent file, and a leftover ``[subagent.*]`` table, stop startup
    the same way.
    """
    if config_tables is None:
        tables = load_config(config_path)
    else:
        # An explicit table is a test. Still refuse a leftover file when given one.
        if config_path is not None:
            refuse_subagent_tables(config_path)

        tables = config_tables

    files = _agent_files(agents_dir)
    missing = sorted(set(tables) - set(files))

    if missing:
        where = config_path or config.config_path()
        listed = ", ".join(f"[agent.{name}]" for name in missing)
        files_needed = ", ".join(f"agents/{name}.md" for name in missing)
        raise SystemExit(f"{where}: {listed} has no agent file; add {files_needed}")

    registered = set(registered_mcp_servers)
    agents: dict[str, Agent] = {}
    warnings: list[str] = []

    for name, path in files.items():
        agent, found = _load_one(path, skills, registered, tables.get(name, {}))
        warnings.extend(found)

        if agent is not None:
            agents[agent.name] = agent

    if on_warning is not None:
        for warning in warnings:
            on_warning(warning)

    return agents


def expand_grants(grants: Iterable[str], mcp_tool_names: Iterable[str]) -> set[str]:
    """Plain names unchanged; ``mcp__<server>__*`` becomes that server's tools.

    A specific ``mcp__<server>__<tool>`` stays as written. Tools of any other
    server are left out. A pattern whose server has not connected expands to
    nothing: the grant is real, the tool is not available yet.
    """
    available = set(mcp_tool_names)
    expanded: set[str] = set()

    for grant in grants:
        match = MCP_GRANT.fullmatch(grant)

        if match and match.group(2) == "*":
            prefix = f"mcp__{match.group(1)}__"
            expanded.update(name for name in available if name.startswith(prefix))
            continue

        expanded.add(grant)

    return expanded


def granted(tools: Iterable[dict], names: Iterable[str]) -> list[dict]:
    """The tool definitions whose names an agent was granted and that exist."""
    allowed = set(names)
    return [tool for tool in tools if tool["function"]["name"] in allowed]


def offered_tools(
    grants: Iterable[str],
    case_tools: Iterable[dict],
    harness_tools: Iterable[dict],
    mcp_tools: Iterable[dict],
) -> list[dict]:
    """Case, harness and MCP definitions the grant allows, that are available.

    A granted tool that is not in these lists (sandbox off, server not
    connected) is left out. Patterns expand against the MCP definitions here.
    """
    grant_list = list(grants)
    mcp_list = list(mcp_tools)
    allowed = expand_grants(grant_list, (tool["function"]["name"] for tool in mcp_list))
    return granted([*case_tools, *harness_tools, *mcp_list], allowed)


def spawn_tool(
    agents: dict[str, Agent],
    mcp_tool_names: Iterable[str] = (),
    *,
    sandbox: bool = False,
) -> dict:
    """The ``spawn_subagent`` tool: one agent name, one task.

    Each agent's line names the tools it can use right now: MCP servers among
    its grants that have connected, and ``exec`` and ``view_image`` when the
    sandbox can run. A server that is down or not registered is left out, and
    so are tools every agent has.
    """
    available = set(mcp_tool_names)
    roster = "; ".join(
        _roster_line(agent, available, sandbox) for agent in agents.values()
    )
    return {
        "type": "function",
        "function": {
            "name": "spawn_subagent",
            "description": (
                "Run an agent in its own context, with the tools that agent is "
                f"granted. It reads the files and returns findings to cite. {roster}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "enum": list(agents)},
                    "task": {
                        "type": "string",
                        "description": "What the agent should answer.",
                    },
                    "context": {
                        "type": "string",
                        "description": "What other findings it needs, if any.",
                    },
                    "resource_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Case resource ids it should use.",
                    },
                },
                "required": ["agent", "task"],
            },
        },
    }


def _agent_files(folder: Path) -> dict[str, Path]:
    """``agents/<name>.md`` in this folder, by the file's stem."""
    if not folder.is_dir():
        return {}

    return {path.stem: path for path in sorted(folder.glob("*.md"))}


def load_skill_note(
    skill: Skill,
    held: Iterable[str],
    agents: dict[str, Agent],
    mcp_tool_names: Iterable[str] = (),
    *,
    sandbox: bool = False,
) -> str | None:
    """When the caller lacks tools a skill needs or uses, who can use them now.

    None when the caller already has every name in ``requires`` and ``uses``.
    An agent is named only for tools it can use now: a grant is not enough, so
    ``exec`` is omitted while the sandbox is off and an MCP server is omitted
    until it has connected. When no agent can use a missing tool, the note
    says why.
    """
    caller = set(held)
    connected = set(mcp_tool_names)
    carriers = [agent for agent in agents.values() if skill.name in agent.skills]
    missing_requires = [
        name for name in skill.requires if not _caller_has(name, caller)
    ]
    missing_uses = [
        name
        for name in skill.uses
        if name not in skill.requires and not _caller_has(name, caller)
    ]
    sentences = _gap_sentences("needs", missing_requires, carriers, connected, sandbox)
    sentences += _gap_sentences("uses", missing_uses, carriers, connected, sandbox)

    if not sentences:
        return None

    return " ".join(sentences)


def _load_one(
    path: Path,
    skills: dict[str, Skill],
    registered: set[str],
    table: dict[str, Any],
) -> tuple[Agent | None, list[str]]:
    """One agent file plus its config override, or None when disabled.

    The second value is the startup warnings: a grant whose MCP server is not
    registered here. The grant is kept.
    """
    fields, body = front_matter(path.read_text())
    name = fields.get("name") or path.stem

    if fields.get("name") and fields["name"] != path.stem:
        raise SystemExit(
            f"{path}: name is {fields['name']!r}; the file is agents/{path.stem}.md"
        )

    skill_names = _comma_list(fields.get("skills", ""))
    unknown = [skill for skill in skill_names if skill not in skills]

    if unknown:
        listed = ", ".join(unknown)
        raise SystemExit(f"{path}: names unknown skill {listed}")

    tool_names = _comma_list(fields.get("tools", ""))
    warnings: list[str] = []

    for tool_name in tool_names:
        warning = _check_grant(path, tool_name, registered)

        if warning is not None and warning not in warnings:
            warnings.append(warning)

    grants = set(tool_names)

    for skill_name in skill_names:
        missing = [
            required
            for required in skills[skill_name].requires
            if not _covers(grants, required)
        ]

        if missing:
            listed = ", ".join(missing)
            raise SystemExit(
                f"{path}: grants do not cover what skill {skill_name} requires: "
                f"{listed}"
            )

    # Disabled after the file is checked, so a typo still stops startup.
    if table.get("enabled", True) is False:
        return None, warnings

    return (
        Agent(
            name=name,
            description=fields.get("description", ""),
            body=body,
            skills=tuple(skill_names),
            tools=tuple(tool_names),
            max_steps=_max_steps(path, fields, table),
            model_overrides=_overrides(fields, table),
        ),
        warnings,
    )


def _check_grant(path: Path, grant: str, registered: set[str]) -> str | None:
    """A plain tool must exist. An unregistered MCP server is a warning.

    The warning is returned, not raised: the grant stays, and expands to
    nothing until that server is registered and connected. ``spawn_subagent``
    is refused, because a subagent cannot spawn.
    """
    match = MCP_GRANT.fullmatch(grant)

    if match:
        server = match.group(1)

        if server not in registered:
            where = f"{path.parent.name}/{path.name}"
            return (
                f"{where} grants {server}, which is not registered here; "
                "its tools are not offered"
            )

        return None

    if grant == "spawn_subagent":
        raise SystemExit(
            f"{path}: grants spawn_subagent, but a subagent cannot spawn, "
            "so it would do nothing"
        )

    if grant not in _grantable():
        raise SystemExit(f"{path}: grants unknown tool {grant!r}")

    return None


def _grantable() -> set[str]:
    """Case and harness tool names, from the registry, excluding MCP tools.

    A new harness tool is grantable without editing this module.
    ``spawn_subagent`` is in the set so its refusal can name it; granting it
    is still an error.
    """
    names: set[str] = set()

    for name in registry.registered():
        source = registry.source_of(name) or ""

        if name.startswith("mcp__"):
            continue

        if source == "datactrl" or source == "harness":
            names.add(name)

    return names


def _covers(grants: set[str], required: str) -> bool:
    """Whether a grant list covers one name a skill requires, patterns included."""
    if required in grants:
        return True

    match = MCP_GRANT.fullmatch(required)

    if match is None:
        return False

    return f"mcp__{match.group(1)}__*" in grants


def _max_steps(path: Path, fields: dict[str, str], table: dict[str, Any]) -> int:
    """The file's budget, then the config's, which wins.

    Zero is an error in either place. A missing key keeps the default.
    """
    steps = DEFAULT_MAX_STEPS

    if fields.get("max_steps"):
        steps = _integer(path, fields["max_steps"], "max_steps")

    if "max_steps" in table:
        steps = _integer(path, str(table["max_steps"]), "max_steps")

    return steps


def _integer(path: Path, raw: str, key: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{path}: {key} must be an integer") from None

    if value < 1:
        raise SystemExit(f"{path}: {key} must be at least 1")

    return value


def _overrides(
    fields: dict[str, str], table: dict[str, Any]
) -> tuple[tuple[str, str], ...]:
    """Model settings from the file, with the config table winning on each key."""
    chosen: dict[str, str] = {}

    for key, field in MODEL_KEYS.items():
        if fields.get(key):
            chosen[field] = str(fields[key])

        if table.get(key):
            chosen[field] = str(table[key])

    return tuple(chosen.items())


def _roster_line(agent: Agent, available: set[str], sandbox: bool) -> str:
    """``name: description``, plus the notable tools that are usable now."""
    notable = _notable(agent, available, sandbox)

    if not notable:
        return f"{agent.name}: {agent.description}"

    summary = agent.description.rstrip(".")
    return f"{agent.name}: {summary}. Can use: {', '.join(notable)}"


def _notable(agent: Agent, available: set[str], sandbox: bool) -> list[str]:
    """MCP servers that have connected, then exec and view_image if the sandbox runs."""
    listed: list[str] = []
    seen: set[str] = set()

    for grant in agent.tools:
        match = MCP_GRANT.fullmatch(grant)

        if match is None:
            continue

        server, tool = match.group(1), match.group(2)

        if server in seen:
            continue

        if tool == "*":
            prefix = f"mcp__{server}__"
            offered = any(name.startswith(prefix) for name in available)
            label = f"{server} ({prefix}*)"
        else:
            offered = f"mcp__{server}__{tool}" in available
            label = f"{server} (mcp__{server}__{tool})"

        if offered:
            listed.append(label)
            seen.add(server)

    if sandbox:
        listed.extend(name for name in NOTABLE_TOOLS if name in agent.tools)

    return listed


def _caller_has(required: str, held: set[str]) -> bool:
    """Whether the caller can use one name a skill requires."""
    if required in held:
        return True

    match = MCP_GRANT.fullmatch(required)

    if match is None or match.group(2) != "*":
        return False

    prefix = f"mcp__{match.group(1)}__"
    return any(name.startswith(prefix) for name in held)


def _gap_sentences(
    label: str,
    names: list[str],
    carriers: list[Agent],
    connected: set[str],
    sandbox: bool,
) -> list[str]:
    """One sentence per outcome for the tools in ``names`` the caller lacks."""
    if not names:
        return []

    usable: list[str] = []
    blocked: dict[str, list[str]] = {}

    for name in names:
        if any(_agent_can_use(agent, name, connected, sandbox) for agent in carriers):
            usable.append(name)
            continue

        blocked.setdefault(
            _unavailable_reason(name, carriers, connected, sandbox), []
        ).append(name)

    sentences: list[str] = []

    if usable:
        sentences.append(_usable_sentence(label, usable, carriers, connected, sandbox))

    disconnected = [
        (reason, group)
        for reason, group in blocked.items()
        if reason.endswith(" is not connected")
    ]
    other = [
        (reason, group)
        for reason, group in blocked.items()
        if not reason.endswith(" is not connected")
    ]

    if disconnected:
        group = [name for _reason, names in disconnected for name in names]
        why = _and([reason for reason, _names in disconnected])
        sentences.append(_cannot(label, group, why))

    sentences.extend(
        _cannot(label, group, _because(reason, group)) for reason, group in other
    )
    return sentences


def _cannot(label: str, names: list[str], why: str) -> str:
    return (
        f"This skill {label} {_and(names)}, which you do not have; "
        f"no agent can use {_it(names)} because {why}."
    )


def _usable_sentence(
    label: str,
    names: list[str],
    carriers: list[Agent],
    connected: set[str],
    sandbox: bool,
) -> str:
    """Name the agent that can use these tools now."""
    full = [
        agent.name
        for agent in carriers
        if all(_agent_can_use(agent, name, connected, sandbox) for name in names)
    ]
    opening = f"This skill {label} {_and(names)}, which you do not have; "

    if full:
        who = full[0] if len(full) == 1 else " or ".join(full)
        return opening + f"spawn_subagent with agent {who} can use {_it(names)}."

    parts: list[str] = []

    for agent in carriers:
        theirs = [
            name for name in names if _agent_can_use(agent, name, connected, sandbox)
        ]

        if theirs:
            parts.append(
                f"spawn_subagent with agent {agent.name} can use {_and(theirs)}"
            )

    return opening + ", and ".join(parts) + "."


def _because(reason: str, names: list[str]) -> str:
    """A reason whose pronoun matches the number of tools it covers."""
    if reason == "no agent grants it" and len(names) > 1:
        return "no agent grants them"

    if reason == "it is not offered" and len(names) > 1:
        return "they are not offered"

    return reason


def _agent_can_use(agent: Agent, name: str, connected: set[str], sandbox: bool) -> bool:
    """Whether this agent is granted the tool and it is offered right now."""
    if not _covers(set(agent.tools), name):
        return False

    if name in NOTABLE_TOOLS:
        return sandbox

    return _connected(name, connected)


def _unavailable_reason(
    name: str, carriers: list[Agent], connected: set[str], sandbox: bool
) -> str:
    """Why no agent can use this tool: not granted, sandbox off, or not connected."""
    granted = any(_covers(set(agent.tools), name) for agent in carriers)

    if not granted:
        return "no agent grants it"

    if name in NOTABLE_TOOLS and not sandbox:
        return "the sandbox is off"

    match = MCP_GRANT.fullmatch(name)

    if match is not None and not _connected(name, connected):
        return f"{match.group(1)} is not connected"

    return "it is not offered"


def _connected(name: str, connected: set[str]) -> bool:
    """A plain tool is available; an MCP name needs a connected tool."""
    match = MCP_GRANT.fullmatch(name)

    if match is None:
        return True

    if match.group(2) == "*":
        prefix = f"mcp__{match.group(1)}__"
        return any(tool.startswith(prefix) for tool in connected)

    return name in connected


def _it(names: list[str]) -> str:
    return "it" if len(names) == 1 else "them"


def _and(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]

    if len(items) == 2:
        return f"{items[0]} and {items[1]}"

    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _comma_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]
