"""Skills read from SKILL.md files, and the tools the harness serves itself."""

from __future__ import annotations

from types import SimpleNamespace

from meddataos.core.datactrl.tools import TOOL_DEFINITIONS
from meddataos.harness.agents import Agent
from meddataos.harness.mcp import McpHub
from meddataos.harness.references import Site
from meddataos.harness.sandbox import SandboxSettings
from meddataos.harness.skills import SKILLS_DIR, catalog, load_skills, parse_skill
from meddataos.harness.tools import HarnessTools

SKILL_TEXT = """---
name: tabular
description: Lab tables and scores
requires: describe, fetch
---
# Tables

Never do arithmetic.
"""


class StubReferences:
    """The reference library's interface, without a network."""

    sites = [Site(name="radiologyassistant", base_url="https://radiologyassistant.nl/")]

    def search(self, query, site=None, limit=5):
        if site == "elsewhere":
            return {"error": "Unknown site 'elsewhere'"}
        return {"query": query, "results": [{"url": "https://radiologyassistant.nl/x"}]}

    def read(self, url, section=None, max_chars=8000):
        raise RuntimeError("network is down")


def tools_with(tmp_path, text=SKILL_TEXT) -> HarnessTools:
    (tmp_path / "tabular").mkdir()
    (tmp_path / "tabular" / "SKILL.md").write_text(text)
    return HarnessTools(skills_dir=tmp_path, references=StubReferences())


def test_front_matter_and_body_are_read():
    skill = parse_skill(SKILL_TEXT, "folder-name")

    assert (skill.name, skill.description, skill.requires) == (
        "tabular",
        "Lab tables and scores",
        ["describe", "fetch"],
    )
    assert skill.body == "# Tables\n\nNever do arithmetic."


def test_a_file_without_front_matter_is_all_body_named_by_its_folder():
    skill = parse_skill("Just instructions.", "notes")

    assert (skill.name, skill.description, skill.body) == (
        "notes",
        "",
        "Just instructions.",
    )


def test_a_wrapped_requires_line_keeps_every_tool():
    skill = parse_skill(
        """---
name: dicom
description: Scans
requires: describe, fetch,
  exec, view_image
---
Read the series.
""",
        "dicom",
    )

    assert skill.requires == ["describe", "fetch", "exec", "view_image"]


def test_a_wrapped_uses_line_keeps_every_tool():
    skill = parse_skill(
        """---
name: dicom
description: Scans
requires: describe, fetch
uses: mcp__totalseg__*,
  mcp__mira-neck__*
---
Read the series.
""",
        "dicom",
    )

    assert skill.uses == ["mcp__totalseg__*", "mcp__mira-neck__*"]


def test_a_tools_line_in_a_skill_warns_and_grants_nothing(tmp_path):
    folder = tmp_path / "skills" / "dicom"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        """---
name: dicom
description: Scans
tools: exec, fetch
requires: describe, fetch
---
Read the series.
"""
    )
    warnings: list[str] = []
    skills = load_skills(folder.parent, on_warning=warnings.append)

    assert skills["dicom"].requires == ["describe", "fetch"]
    assert warnings == [
        "skills/dicom/SKILL.md has tools:, which grants nothing; "
        "grants belong in agents/<name>.md and the skill's needs in requires:"
    ]


def test_the_catalog_lists_names_and_when_to_load_them(tmp_path):
    skills = tools_with(tmp_path).skills
    text = catalog(skills)

    assert text.startswith("Skills: tabular: Lab tables and scores.")
    assert "your own tools" in text
    assert "follow what it says" not in text
    assert catalog({}) == ""


def test_load_skill_returns_the_instructions(tmp_path):
    tools = tools_with(tmp_path)
    loaded = tools.run("load_skill", {"name": "tabular"})
    unknown = tools.run("load_skill", {"name": "pathology"})

    assert loaded == {
        "status": "ok",
        "name": "load_skill",
        "result": {
            "name": "tabular",
            "requires": ["describe", "fetch"],
            "instructions": "# Tables\n\nNever do arithmetic.",
        },
    }
    assert "note" not in loaded["result"]
    assert unknown["status"] == "error" and "Skills: tabular" in unknown["message"]


def _dicom_tools(tmp_path, text: str) -> HarnessTools:
    folder = tmp_path / "dicom"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(text)
    return HarnessTools(
        skills_dir=tmp_path,
        references=StubReferences(),
        sandbox=SandboxSettings("off", "runsc", "image", tmp_path, "4g", "2", 30),
        mcp=McpHub([], trust_store=tmp_path / "trust.json"),
    )


def test_load_skill_names_the_agent_for_optional_tools_it_can_use(tmp_path):
    tools = _dicom_tools(
        tmp_path,
        """---
name: dicom
description: Scans
requires: describe, fetch
uses: mcp__totalseg__*, mcp__mira-neck__*
---
Read the series.
""",
    )
    tools.agents = {
        "dicom": Agent(
            "dicom",
            "Scans",
            "",
            ("dicom",),
            ("describe", "fetch", "exec", "mcp__totalseg__*", "mcp__mira-neck__*"),
        )
    }
    down = tools.run("load_skill", {"name": "dicom"})["result"]
    tools.mcp.tools["mcp__totalseg__segment"] = SimpleNamespace(
        name="mcp__totalseg__segment",
        server=SimpleNamespace(router=False),
        definition={
            "type": "function",
            "function": {"name": "mcp__totalseg__segment"},
        },
    )
    connected = tools.run("load_skill", {"name": "dicom"})["result"]

    assert down["note"] == (
        "This skill uses mcp__totalseg__* and mcp__mira-neck__*, which you do "
        "not have; no agent can use them because totalseg is not connected and "
        "mira-neck is not connected."
    )
    assert "exec" not in down["note"]
    assert connected["note"] == (
        "This skill uses mcp__totalseg__*, which you do not have; "
        "spawn_subagent with agent dicom can use it. "
        "This skill uses mcp__mira-neck__*, which you do not have; "
        "no agent can use it because mira-neck is not connected."
    )
    assert connected["note"] in connected["instructions"]


def test_load_skill_does_not_offer_exec_while_the_sandbox_is_off(tmp_path):
    tools = _dicom_tools(
        tmp_path,
        """---
name: dicom
description: Scans
requires: describe, fetch, exec
---
Read the series.
""",
    )
    tools.agents = {
        "dicom": Agent("dicom", "Scans", "", ("dicom",), ("describe", "fetch", "exec"))
    }
    note = tools.run("load_skill", {"name": "dicom"})["result"]["note"]
    uncovered = _dicom_tools(
        tmp_path / "other",
        """---
name: dicom
description: Scans
requires: describe, fetch, exec
---
Read the series.
""",
    ).run("load_skill", {"name": "dicom"})["result"]["note"]

    assert note == (
        "This skill needs exec, which you do not have; "
        "no agent can use it because the sandbox is off."
    )
    assert "spawn_subagent" not in note
    assert uncovered == (
        "This skill needs exec, which you do not have; "
        "no agent can use it because no agent grants it."
    )


def test_reference_refusals_and_failures_come_back_as_errors(tmp_path):
    tools = tools_with(tmp_path)

    assert tools.run("reference_search", {"query": "li-rads"})["status"] == "ok"
    assert tools.run("reference_search", {"query": "x", "site": "elsewhere"}) == {
        "status": "error",
        "name": "reference_search",
        "message": "Unknown site 'elsewhere'",
    }
    assert (
        tools.run("reference_read", {"url": "https://radiologyassistant.nl/x"})[
            "message"
        ]
        == "network is down"
    )
    assert tools.run("exec", {})["status"] == "error"


def test_load_skill_is_offered_only_when_there_are_skills(tmp_path):
    with_skills = tools_with(tmp_path)
    without = HarnessTools(skills_dir=tmp_path / "none", references=StubReferences())

    assert with_skills.names == {"reference_search", "reference_read", "load_skill"}
    assert without.names == {"reference_search", "reference_read"}
    assert (
        "radiologyassistant" in with_skills.definitions()[0]["function"]["description"]
    )


def test_the_shipped_skills_name_only_tools_that_exist():
    known = {tool["function"]["name"] for tool in TOOL_DEFINITIONS}
    known |= {"reference_search", "reference_read", "load_skill", "exec", "view_image"}
    known |= {"ledger_query", "notes_timeline", "notes_conflicts"}
    skills = load_skills(SKILLS_DIR)

    assert "dicom" in skills
    assert skills["dicom"].uses == ["mcp__totalseg__*", "mcp__mira-neck__*"]

    for skill in skills.values():
        assert skill.description, skill.name
        assert set(skill.requires) <= known, (skill.name, set(skill.requires) - known)
