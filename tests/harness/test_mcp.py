"""MCP servers: namespaced tools, workspace paths, failures and grants.

The server is tests/harness/fixtures/fake_mcp_server.py, run over stdio, so
these tests start a real MCP session without any model or network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from meddataos.core import registry
from meddataos.core.registry import Evidence
from meddataos.harness.mcp import (
    McpError,
    McpHub,
    relativize,
    resolve_paths,
    with_path_defaults,
)
from meddataos.harness.mcp_config import ServerConfig
from meddataos.harness.tools import HarnessTools

FAKE_SERVER = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def fake_server(name: str = "seg", **overrides) -> ServerConfig:
    settings = {
        "name": name,
        "command": (sys.executable, str(FAKE_SERVER)),
        "evidence": Evidence.DERIVED,
        "timeout": 20.0,
    }
    settings.update(overrides)
    return ServerConfig(**settings)


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    trust_store = tmp_path_factory.mktemp("trust") / "mcp_trust.json"
    started = McpHub(
        [fake_server(), fake_server("quick", router=True, timeout=1.0)],
        trust_store=trust_store,
    )
    started.start()
    yield started
    started.stop()


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "volumes").mkdir()
    (tmp_path / "volumes" / "ct.nii.gz").write_bytes(b"volume")
    return tmp_path


# -- workspace paths --------------------------------------------------------


def test_workspace_paths_are_made_absolute_and_back(workspace):
    resolved = resolve_paths(
        {"image": "volumes/ct.nii.gz", "label": "GTVp", "out": ["out/mask.nii.gz"]},
        workspace,
    )

    assert resolved["image"] == str(workspace.resolve() / "volumes" / "ct.nii.gz")
    assert resolved["label"] == "GTVp"
    assert (workspace / "out").is_dir()
    assert (
        relativize(f"mask: {resolved['out'][0]}", workspace) == "mask: out/mask.nii.gz"
    )


def test_a_path_that_leaves_the_workspace_is_refused(workspace):
    with pytest.raises(McpError, match="outside the workspace"):
        resolve_paths({"out": "out/../../secrets.txt"}, workspace)


# -- the hub ----------------------------------------------------------------


def test_tools_are_namespaced_and_registered_with_the_servers_class(hub):
    assert hub.status["seg"] == "connected (9 tools)"
    assert "mcp__seg__segment" in hub.tools
    assert registry.evidence_of("mcp__seg__segment") is Evidence.DERIVED
    assert registry.source_of("mcp__seg__segment") == "mcp:seg"

    definition = hub.tools["mcp__seg__segment"].definition["function"]
    assert definition["description"].startswith("[seg] Segment the primary tumour")
    assert set(definition["parameters"]["properties"]) == {"image", "out"}


def test_a_call_reads_and_writes_workspace_paths(hub, workspace):
    result = hub.call(
        "mcp__seg__segment",
        {"image": "volumes/ct.nii.gz", "out": "out/mask.nii.gz"},
        workspace,
    )

    assert result["text"] == "GTVp volume: 2.25 mL\nmask: out/mask.nii.gz"
    assert (workspace / "out" / "mask.nii.gz").read_text() == "mask"


def test_a_servers_error_comes_back_as_an_error(hub, workspace):
    with pytest.raises(McpError, match="model weights missing"):
        hub.call("mcp__seg__broken", {}, workspace)


def test_a_call_past_the_timeout_is_stopped(hub, workspace):
    with pytest.raises(McpError):
        hub.call("mcp__quick__stall", {"seconds": 5}, workspace)


def test_a_returned_picture_is_attached_for_the_model(hub, workspace):
    result = hub.call("mcp__seg__overlay", {}, workspace)

    assert result["attach_image"]["mime_type"] == "image/png"
    assert result["attach_image"]["image_base64"]


def test_a_server_that_cannot_start_is_a_status_not_a_crash(tmp_path):
    broken = McpHub(
        [fake_server("gone", command=("/nonexistent/seg-mcp",))],
        trust_store=tmp_path / "trust.json",
    )
    broken.start(timeout=20)

    try:
        assert broken.status["gone"].startswith("failed:")
        assert broken.definitions_for({"mcp__gone__*"}) == []
    finally:
        broken.stop()


# -- grants -----------------------------------------------------------------


def test_a_pattern_expands_to_that_server_and_the_router_flag_is_separate(hub):
    seg = hub.names_for({"mcp__seg__*"})

    assert "mcp__seg__segment" in seg
    assert not any(name.startswith("mcp__quick__") for name in seg)
    assert hub.names_for({"describe"}) == set()
    assert "mcp__seg__segment" not in hub.names_for("router")
    assert hub.names_for("router") == {
        f"mcp__quick__{tool}"
        for tool in (
            "segment",
            "measure",
            "broken",
            "stall",
            "crash",
            "oom",
            "locate",
            "save",
            "overlay",
        )
    }


def test_harness_tools_run_mcp_tools_and_show_their_pictures(hub, workspace, tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    tools = HarnessTools(skills_dir=skills, mcp=hub)

    assert "mcp__seg__segment" not in tools.names
    assert "mcp__quick__segment" in tools.names

    ran = tools.run("mcp__seg__overlay", {}, workspace)
    failed = tools.run("mcp__seg__broken", {}, workspace)

    assert ran["status"] == "ok" and ran["attach_image"]["path"] == "returned image"
    assert "attach_image" not in ran["result"]
    assert failed == {
        "status": "error",
        "name": "mcp__seg__broken",
        "message": failed["message"],
    }
    assert "model weights missing" in failed["message"]


def test_an_omitted_output_folder_still_lands_in_the_workspace(hub, workspace):
    result = hub.call("mcp__seg__save", {"label": "GTVp"}, workspace)

    assert (workspace / "out" / "masks" / "GTVp.nii.gz").exists()
    assert "out/masks/GTVp.nii.gz" in result["text"]
    assert str(workspace) not in result["text"]


def test_only_relative_path_defaults_are_filled():
    schema = {
        "properties": {
            "out_dir": {"default": "out/masks"},
            "model_path": {"default": "/opt/weights"},
            "mask_path": {"default": None},
            "image_path": {},
        }
    }
    marked = frozenset(schema["properties"])

    assert with_path_defaults({}, marked, schema) == {"out_dir": "out/masks"}
    assert with_path_defaults({"out_dir": "out/x"}, marked, schema) == {
        "out_dir": "out/x"
    }
