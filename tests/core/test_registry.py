"""Every tool's evidence class, and why an unknown tool is never evidence."""

from __future__ import annotations

import pytest
from meddataos.core import registry
from meddataos.core.datactrl.tools import TOOL_DEFINITIONS
from meddataos.core.registry import Evidence, evidence_of


def test_every_offered_tool_has_a_class():
    offered = {tool["function"]["name"] for tool in TOOL_DEFINITIONS}
    offered |= {
        "reference_search",
        "reference_read",
        "load_skill",
        "exec",
        "view_image",
        "ledger_query",
        "notes_timeline",
        "notes_conflicts",
        "spawn_subagent",
    }

    assert offered <= set(registry.registered())


def test_only_case_reads_and_derived_runs_can_be_evidence():
    # MCP servers started by other tests register their own tools; leave them out.
    citable = {
        name
        for name, evidence in registry.registered().items()
        if evidence in (Evidence.CASE, Evidence.DERIVED)
        and not str(registry.source_of(name)).startswith("mcp:")
    }

    assert citable == {"describe", "fetch", "exec"}
    assert evidence_of("reference_read") is Evidence.BACKGROUND


def test_an_unknown_tool_is_background_never_evidence():
    assert evidence_of("mcp__someone__something") is Evidence.BACKGROUND
    assert registry.source_of("mcp__someone__something") is None


def test_two_sources_cannot_disagree_about_a_tool():
    registry.register("exec", Evidence.DERIVED, "harness")

    with pytest.raises(ValueError, match="'exec' is derived from harness"):
        registry.register("exec", Evidence.CASE, "mcp:elsewhere")


def test_the_prompt_names_what_can_be_cited_among_the_offered_tools():
    rule = registry.citing_rule(
        ["describe", "fetch", "exec", "reference_read", "load_skill"]
    )

    assert rule.startswith(
        "Cite only these: case reads (describe, fetch) and, as derived values, runs of exec."
    )
    assert "reference_read give background, never evidence" in rule
    assert "exec" not in registry.citing_rule(["describe", "fetch"])
