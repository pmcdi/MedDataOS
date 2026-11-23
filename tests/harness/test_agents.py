"""Agents: files, grants, startup checks, and what the router is offered."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from meddataos.core.model import ResponsesClient
from meddataos.core.registry import Evidence, register
from meddataos.harness.agents import (
    AGENTS_DIR,
    expand_grants,
    load_agents,
    offered_tools,
    spawn_tool,
)
from meddataos.harness.mcp import McpHub
from meddataos.harness.mcp_config import ServerConfig
from meddataos.harness.skills import Skill, load_skills
from meddataos.harness.tools import HarnessTools
from test_skills_and_tools import StubReferences

SKILLS = {
    "dicom": Skill("dicom", "Scans", "Read the series.", ["dicom_tree", "fetch"]),
    "notes": Skill("notes", "Notes", "Quote the notes.", ["notes_search", "fetch"]),
}


def write_agent(folder: Path, name: str, body: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(body)


def dicom_file(tools: str = "dicom_tree, fetch", extra: str = "") -> str:
    return f"""---
name: dicom
description: Scans
skills: dicom
tools: {tools}
{extra}---
"""


def test_an_agent_file_is_its_grant_and_its_skill(tmp_path):
    folder = tmp_path / "agents"
    write_agent(folder, "dicom", dicom_file(extra="max_steps: 24\n"))
    write_agent(
        folder,
        "notes",
        """---
name: notes
description: Notes
skills: notes
tools: notes_search, fetch
---
""",
    )
    agents = load_agents(folder, SKILLS, [], {})

    assert list(agents) == ["dicom", "notes"]
    assert agents["dicom"].tools == ("dicom_tree", "fetch")
    assert agents["dicom"].max_steps == 24
    assert agents["dicom"].system_prompt("RULES", SKILLS) == (
        "RULES\n\nRead the series."
    )


def test_a_config_table_overrides_the_file_and_can_disable_one(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("OPENAI_MODEL", "base-model")
    monkeypatch.setenv("HARNESS_MODEL_API", "chat")
    folder = tmp_path / "agents"
    write_agent(folder, "dicom", dicom_file(extra="max_steps: 8\nmodel: file-model\n"))
    write_agent(
        folder,
        "notes",
        """---
name: notes
description: Notes
skills: notes
tools: notes_search, fetch
---
""",
    )
    agents = load_agents(
        folder,
        SKILLS,
        [],
        {
            "dicom": {"model_api": "responses", "model": "big-model", "max_steps": 24},
            "notes": {"enabled": False},
        },
    )

    dicom = agents["dicom"].client()

    assert list(agents) == ["dicom"]
    assert isinstance(dicom, ResponsesClient) and dicom.settings.model == "big-model"
    assert agents["dicom"].max_steps == 24


def test_two_skills_are_preloaded_in_order(tmp_path):
    folder = tmp_path / "agents"
    write_agent(
        folder,
        "both",
        """---
name: both
description: Both
skills: dicom, notes
tools: dicom_tree, fetch, notes_search
---
Look here first.
""",
    )
    both = load_agents(folder, SKILLS, [], {})["both"]
    prompt = both.system_prompt("RULES", SKILLS)

    assert prompt == (
        "RULES\n\nLook here first.\n\nRead the series.\n\nQuote the notes."
    )
    assert both.tools == ("dicom_tree", "fetch", "notes_search")


def test_a_wildcard_expands_to_that_server_and_nothing_else():
    expanded = expand_grants(
        ["fetch", "mcp__seg__*"],
        ["mcp__seg__segment", "mcp__seg__measure", "mcp__other__segment"],
    )

    assert expanded == {"fetch", "mcp__seg__segment", "mcp__seg__measure"}


def test_an_agent_that_does_not_grant_a_server_never_sees_its_tools():
    mcp = [
        {"type": "function", "function": {"name": "mcp__seg__segment"}},
        {"type": "function", "function": {"name": "mcp__other__segment"}},
    ]
    case = [{"type": "function", "function": {"name": "fetch"}}]
    granted = [
        tool["function"]["name"]
        for tool in offered_tools(["fetch", "mcp__seg__*"], case, [], mcp)
    ]
    refused = [
        tool["function"]["name"] for tool in offered_tools(["fetch"], case, [], mcp)
    ]

    assert granted == ["fetch", "mcp__seg__segment"]
    assert refused == ["fetch"]


def test_router_tools_include_a_server_only_when_it_is_marked(monkeypatch, tmp_path):
    from meddataos.server import dispatch
    from meddataos.server.state import state

    hub = McpHub([], trust_store=tmp_path / "trust.json")
    hub._register(
        ServerConfig(name="alpha"),
        [SimpleNamespace(name="segment", description="Segment.", input_schema={})],
    )
    hub._register(
        ServerConfig(name="beta", router=True),
        [SimpleNamespace(name="segment", description="Segment.", input_schema={})],
    )
    tools = HarnessTools(
        skills_dir=tmp_path / "skills", references=StubReferences(), mcp=hub
    )
    monkeypatch.setattr(dispatch, "harness_tools", lambda: tools)
    monkeypatch.setattr(dispatch, "offered_agents", lambda: {})
    monkeypatch.setattr(state, "harness", True)
    names = {tool["function"]["name"] for tool in dispatch.router_tools()}

    assert "mcp__beta__segment" in names
    assert "mcp__alpha__segment" not in names


def test_an_unknown_skill_stops_startup(tmp_path):
    folder = tmp_path / "agents"
    write_agent(
        folder, "dicom", dicom_file().replace("skills: dicom", "skills: pathology")
    )

    with pytest.raises(SystemExit, match=r"dicom.md: names unknown skill pathology"):
        load_agents(folder, SKILLS, [], {})


def test_an_unknown_tool_stops_startup(tmp_path):
    folder = tmp_path / "agents"
    write_agent(folder, "dicom", dicom_file("nope"))

    with pytest.raises(SystemExit, match=r"dicom.md: grants unknown tool 'nope'"):
        load_agents(folder, SKILLS, [], {})


def test_an_unregistered_mcp_server_warns_and_offers_nothing(tmp_path):
    folder = tmp_path / "agents"
    write_agent(folder, "dicom", dicom_file("dicom_tree, fetch, mcp__mira-neck__*"))
    warnings: list[str] = []
    agents = load_agents(folder, SKILLS, ["totalseg"], {}, on_warning=warnings.append)
    case = [
        {"type": "function", "function": {"name": "dicom_tree"}},
        {"type": "function", "function": {"name": "fetch"}},
    ]
    offered = [
        tool["function"]["name"]
        for tool in offered_tools(agents["dicom"].tools, case, [], [])
    ]

    assert "mcp__mira-neck__*" in agents["dicom"].tools
    assert offered == ["dicom_tree", "fetch"]
    assert warnings == [
        "agents/dicom.md grants mira-neck, which is not registered here; "
        "its tools are not offered"
    ]


def test_an_unregistered_grant_is_logged_once(monkeypatch, tmp_path):
    from meddataos.server import dispatch

    config = tmp_path / "harness.toml"
    config.write_text("")
    monkeypatch.setenv("HARNESS_CONFIG", str(config))
    skills = load_skills()
    harness = SimpleNamespace(skills=skills, mcp=SimpleNamespace(servers={}), agents={})
    notes: list[str] = []
    previous = dispatch._agents
    monkeypatch.setattr(dispatch, "harness_tools", lambda: harness)
    monkeypatch.setattr(dispatch, "log", lambda who, message: notes.append(message))
    dispatch._agents = None

    try:
        first = dispatch.offered_agents()
        again = dispatch.offered_agents()
    finally:
        dispatch._agents = previous

    assert first is again
    assert (
        "agents/dicom.md grants mira-neck, which is not registered here; "
        "its tools are not offered"
    ) in notes
    assert (
        "agents/dicom.md grants totalseg, which is not registered here; "
        "its tools are not offered"
    ) in notes
    assert len(notes) == 2


def test_spawn_subagent_cannot_be_granted(tmp_path):
    folder = tmp_path / "agents"
    write_agent(folder, "dicom", dicom_file("dicom_tree, fetch, spawn_subagent"))

    with pytest.raises(
        SystemExit, match=r"a subagent cannot spawn, so it would do nothing"
    ):
        load_agents(folder, SKILLS, [], {})


def test_a_newly_registered_tool_is_grantable(tmp_path):
    register("fresh_probe", Evidence.NAVIGATION, "harness")
    folder = tmp_path / "agents"
    write_agent(folder, "dicom", dicom_file("dicom_tree, fetch, fresh_probe"))

    try:
        agents = load_agents(folder, SKILLS, [], {})
    finally:
        from meddataos.core import registry

        registry._REGISTRY.pop("fresh_probe", None)

    assert "fresh_probe" in agents["dicom"].tools


def test_max_steps_zero_is_rejected_in_the_file_and_the_config(tmp_path):
    folder = tmp_path / "agents"
    write_agent(folder, "dicom", dicom_file(extra="max_steps: 0\n"))

    with pytest.raises(SystemExit, match=r"dicom.md: max_steps must be at least 1"):
        load_agents(folder, SKILLS, [], {})

    write_agent(folder, "dicom", dicom_file())

    with pytest.raises(SystemExit, match=r"dicom.md: max_steps must be at least 1"):
        load_agents(folder, SKILLS, [], {"dicom": {"max_steps": 0}})


def test_a_wrapped_tools_line_is_one_grant(tmp_path):
    folder = tmp_path / "agents"
    write_agent(
        folder,
        "dicom",
        """---
name: dicom
description: Scans
skills: dicom
tools: dicom_tree, fetch,
  mcp__totalseg__*
---
""",
    )
    agents = load_agents(folder, SKILLS, ["totalseg"], {})

    assert agents["dicom"].tools == ("dicom_tree", "fetch", "mcp__totalseg__*")


def test_a_grant_must_cover_what_the_skill_requires(tmp_path):
    folder = tmp_path / "agents"
    write_agent(folder, "dicom", dicom_file("dicom_tree"))

    with pytest.raises(
        SystemExit, match=r"do not cover what skill dicom requires: fetch"
    ):
        load_agents(folder, SKILLS, [], {})


def test_a_pattern_covers_a_skill_requirement(tmp_path):
    skills = {
        "dicom": Skill(
            "dicom", "Scans", "Read the series.", ["fetch", "mcp__seg__segment"]
        )
    }
    folder = tmp_path / "agents"
    write_agent(folder, "dicom", dicom_file("fetch, mcp__seg__*"))
    agent = load_agents(folder, skills, ["seg"], {})["dicom"]

    assert "mcp__seg__*" in agent.tools


def test_a_config_table_with_no_agent_file_stops_startup(tmp_path):
    config = tmp_path / "harness.toml"
    config.write_text("[agent.pathology]\nmax_steps = 4\n")
    (tmp_path / "agents").mkdir()

    with pytest.raises(
        SystemExit, match=r"\[agent.pathology\] has no agent file"
    ) as caught:
        load_agents(tmp_path / "agents", SKILLS, [], config_path=config)

    assert "agents/pathology.md" in str(caught.value)


def test_a_leftover_subagent_table_points_at_agents(tmp_path):
    config = tmp_path / "harness.toml"
    config.write_text("[subagent.dicom]\nmax_steps = 4\n")

    with pytest.raises(
        SystemExit, match=r"\[subagent.dicom\] moved to \[agent"
    ) as caught:
        load_agents(tmp_path / "agents", SKILLS, [], config_path=config)

    assert "agents/" in str(caught.value)


def test_the_spawn_tool_names_each_agent(tmp_path):
    folder = tmp_path / "agents"
    write_agent(folder, "dicom", dicom_file())
    write_agent(
        folder,
        "notes",
        """---
name: notes
description: Notes
skills: notes
tools: notes_search, fetch
---
""",
    )
    tool = spawn_tool(load_agents(folder, SKILLS, [], {}))["function"]

    assert tool["name"] == "spawn_subagent"
    assert tool["parameters"]["properties"]["agent"]["enum"] == ["dicom", "notes"]
    assert "skill" not in tool["parameters"]["properties"]
    assert "dicom: Scans" in tool["description"]
    assert "Can use" not in tool["description"]


def test_the_roster_names_only_the_tools_an_agent_can_use_now(tmp_path):
    folder = tmp_path / "agents"
    write_agent(
        folder,
        "dicom",
        dicom_file(
            "dicom_tree, fetch, exec, view_image, "
            "mcp__totalseg__*, mcp__mira-neck__*, mcp__down__*"
        ),
    )
    warnings: list[str] = []
    agents = load_agents(
        folder,
        SKILLS,
        ["totalseg", "mira-neck"],
        {},
        on_warning=warnings.append,
    )
    connected = spawn_tool(
        agents,
        ["mcp__totalseg__segment", "mcp__mira-neck__infer"],
        sandbox=True,
    )["function"]["description"]
    quiet = spawn_tool(agents, [], sandbox=False)["function"]["description"]

    assert (
        "dicom: Scans. Can use: totalseg (mcp__totalseg__*), "
        "mira-neck (mcp__mira-neck__*), exec, view_image"
    ) in connected
    assert "down" not in connected
    assert "fetch" not in connected
    assert "Can use" not in quiet
    assert "exec" not in quiet
    assert any("grants down" in warning for warning in warnings)


def test_the_shipped_agents_keep_today_behaviour():
    skills = load_skills()
    agents = load_agents(AGENTS_DIR, skills, ["totalseg", "mira-neck"], {})

    assert set(agents) == {"dicom", "notes", "tabular"}
    assert agents["dicom"].max_steps == 24
    assert "mcp__totalseg__*" in agents["dicom"].tools
    assert "mcp__mira-neck__*" in agents["dicom"].tools

    for name in ("dicom", "notes", "tabular"):
        prompt = agents[name].system_prompt("RULES", skills)

        assert agents[name].description == skills[name].description
        assert prompt.startswith("RULES\n\n")
        assert prompt.endswith(skills[name].body)
        assert set(skills[name].requires) <= set(agents[name].tools)
