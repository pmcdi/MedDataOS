"""Registering MCP servers: the files and scopes, approval, images and the CLI.

Every test points the three scopes, the harness config and the trust store at
a temporary folder, so nothing here reads or writes the developer's own.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from meddataos.core.registry import Evidence
from meddataos.harness import mcp, mcp_config
from meddataos.harness.mcp import McpHub, container_command, launch_problem
from meddataos.harness.mcp_cli import main as mcp_cli
from meddataos.harness.mcp_config import (
    ApprovalNeeded,
    McpConfigError,
    Scope,
    check_approval,
    load_servers,
    parse_entry,
    trust,
)
from meddataos.harness.sandbox import SandboxSettings

FAKE_SERVER = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


@pytest.fixture
def scopes(tmp_path, monkeypatch):
    files = {
        Scope.USER: tmp_path / "user" / "mcp.json",
        Scope.PROJECT: tmp_path / "configs" / "mcp.json",
        Scope.LOCAL: tmp_path / "configs" / "mcp.local.json",
    }
    monkeypatch.setenv("HARNESS_MCP_USER", str(files[Scope.USER]))
    monkeypatch.setenv("HARNESS_MCP_PROJECT", str(files[Scope.PROJECT]))
    monkeypatch.setenv("HARNESS_MCP_LOCAL", str(files[Scope.LOCAL]))
    empty = tmp_path / "harness.toml"
    empty.write_text("")
    monkeypatch.setenv("HARNESS_CONFIG", str(empty))
    monkeypatch.setattr(mcp_config, "TRUST_STORE", tmp_path / "trust.json")
    return files


def register(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}))


FAKE = {"command": sys.executable, "args": [str(FAKE_SERVER)]}


# -- entries ----------------------------------------------------------------


def test_an_entry_in_claude_codes_shape_is_read_with_our_defaults():
    server = parse_entry("seg", {"command": "seg-mcp", "args": ["--gpu"]})

    assert server.command == ("seg-mcp", "--gpu")
    assert server.evidence is Evidence.BACKGROUND
    assert server.router is False
    assert server.workspace_paths and not server.sandbox


@pytest.mark.parametrize(
    ("entry", "complaint"),
    [
        ({"type": "http"}, "needs a url"),
        ({}, "needs a command"),
        ({"type": "sse", "url": "http://x"}, "not supported"),
        ({"command": ["seg-mcp"]}, "command must be a string"),
        (
            {"url": "http://x", "type": "http", "headers": {"A": "b"}},
            "headers are not supported",
        ),
        ({"command": "x", "meddataos": {"evidence": "case"}}, "not offered yet"),
        ({"command": "x", "meddataos": {"evidence": "proof"}}, "unknown evidence"),
        (
            {"command": "x", "meddataos": {"sandbox_mode": True}},
            "unknown meddataos keys",
        ),
        ({"command": "x", "meddataos": {"skills": ["dicom"]}}, "Grants moved"),
        ({"meddataos": {"image": "seg:1", "sandbox": False}}, "always runs sandboxed"),
    ],
)
def test_a_bad_entry_says_why(entry, complaint):
    with pytest.raises(McpConfigError, match=complaint):
        parse_entry("seg", entry)


def test_a_bad_name_is_refused():
    with pytest.raises(McpConfigError, match="lowercase"):
        parse_entry("Seg", {"command": "x"})


def test_environment_references_are_filled_in(monkeypatch):
    monkeypatch.setenv("SEG_HOME", "/opt/seg")
    monkeypatch.delenv("SEG_MISSING", raising=False)
    server = parse_entry(
        "seg",
        {
            "command": "${SEG_HOME}/bin/seg",
            "env": {"WEIGHTS": "${SEG_MISSING:-/weights}"},
        },
    )

    assert server.command == ("/opt/seg/bin/seg",)
    assert dict(server.env) == {"WEIGHTS": "/weights"}


def test_an_image_alone_is_enough_and_runs_sandboxed():
    server = parse_entry(
        "seg", {"meddataos": {"image": "meddataos/totalseg-mcp:2.18.0"}}
    )

    assert server.sandbox and server.command == ()


# -- scopes -----------------------------------------------------------------


def test_the_narrowest_scope_wins_on_a_name(scopes):
    register(
        scopes[Scope.USER], {"seg": {"command": "user-seg"}, "notes": {"command": "n"}}
    )
    register(scopes[Scope.LOCAL], {"seg": {"command": "local-seg"}})

    servers = {server.name: server for server in load_servers()}

    assert (
        servers["seg"].command == ("local-seg",) and servers["seg"].scope is Scope.LOCAL
    )
    assert servers["notes"].scope is Scope.USER


def test_tables_left_in_harness_toml_are_pointed_at_the_new_files(
    scopes, tmp_path, monkeypatch
):
    old = tmp_path / "old.toml"
    old.write_text('[mcp.seg]\ncommand = ["x"]\n')
    monkeypatch.setenv("HARNESS_CONFIG", str(old))

    with pytest.raises(McpConfigError, match="moved to configs/mcp.json"):
        load_servers()


# -- approval ---------------------------------------------------------------


def test_a_project_server_needs_approval_and_again_after_an_edit(scopes):
    register(scopes[Scope.PROJECT], {"seg": FAKE})
    (server,) = load_servers()

    with pytest.raises(ApprovalNeeded, match="not approved here"):
        check_approval(server)

    trust(server)
    check_approval(server)

    # Someone grants it citable output in the shared file.
    register(
        scopes[Scope.PROJECT], {"seg": {**FAKE, "meddataos": {"evidence": "derived"}}}
    )
    (edited,) = load_servers()

    with pytest.raises(ApprovalNeeded, match="changed since it was approved"):
        check_approval(edited)


def test_servers_you_registered_yourself_need_no_approval(scopes):
    register(scopes[Scope.USER], {"seg": FAKE})
    (server,) = load_servers()

    check_approval(server)


def test_an_unapproved_project_server_does_not_start(scopes, tmp_path):
    register(
        scopes[Scope.PROJECT], {"gated": {**FAKE, "meddataos": {"evidence": "derived"}}}
    )
    (server,) = load_servers()
    hub = McpHub([server], trust_store=tmp_path / "trust.json")
    hub.start()

    try:
        assert hub.status["gated"].startswith("awaiting approval: it comes from")
        assert hub.tools == {}
    finally:
        hub.stop()

    trust(server, tmp_path / "trust.json")
    approved = McpHub([server], trust_store=tmp_path / "trust.json")
    approved.start()

    try:
        assert approved.status["gated"].startswith("connected")
    finally:
        approved.stop()


# -- images -----------------------------------------------------------------


def sandbox_settings() -> SandboxSettings:
    return SandboxSettings(
        backend="off",
        runtime="runsc",
        image="debian:bookworm-slim",
        python_env=Path("/nowhere"),
        memory="4g",
        cpus="2",
        timeout=30,
    )


def test_an_image_server_runs_its_own_image_and_is_never_pulled(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "WORKSPACES", tmp_path / "workspaces")
    server = parse_entry(
        "seg", {"meddataos": {"image": "meddataos/totalseg-mcp:2.18.0"}}
    )

    command = container_command(server, sandbox_settings(), "meddataos-mcp-seg-1")

    assert command[-1] == "meddataos/totalseg-mcp:2.18.0"
    assert "--pull=never" in command and "--network=none" in command
    # The image keeps its own PATH and working folder.
    assert not any(flag.startswith(("--workdir", "--env=PATH")) for flag in command)


def test_a_missing_image_is_a_reason_not_a_download(monkeypatch):
    monkeypatch.setattr(mcp, "runtime_problem", lambda settings: None)
    monkeypatch.setattr(mcp, "image_present", lambda image: False)
    server = parse_entry(
        "seg", {"meddataos": {"image": "meddataos/totalseg-mcp:2.18.0"}}
    )

    assert "is not on this machine" in launch_problem(server, sandbox_settings())


# -- the CLI ----------------------------------------------------------------


def test_add_list_get_trust_and_remove(scopes, capsys):
    added = mcp_cli(
        [
            "add",
            "seg",
            "--scope",
            "project",
            "--evidence",
            "derived",
            "--router",
            "--sandbox",
            "--mount",
            "~/weights:/weights:ro",
            "--env",
            "TOKEN=secret",
            "--timeout",
            "1800",
            "--",
            "python",
            "servers/seg/server.py",
        ]
    )
    entry = json.loads(scopes[Scope.PROJECT].read_text())["mcpServers"]["seg"]

    assert added == 0
    assert entry["command"] == "python" and entry["args"] == ["servers/seg/server.py"]
    assert (
        entry["meddataos"]["timeout"] == 1800 and entry["meddataos"]["sandbox"] is True
    )
    assert entry["meddataos"]["router"] is True
    assert "mcp trust seg" in capsys.readouterr().out

    assert mcp_cli(["list"]) == 0
    assert "needs approval" in capsys.readouterr().out

    assert mcp_cli(["get", "seg"]) == 0
    shown = capsys.readouterr().out
    # Env keys are shown for review; their values may be secrets.
    assert "TOKEN" in shown.split("\n\n")[0] and "secret" not in shown.split("\n\n")[0]

    assert mcp_cli(["trust", "seg"]) == 0
    capsys.readouterr()
    assert mcp_cli(["list"]) == 0
    assert "needs approval" not in capsys.readouterr().out

    assert mcp_cli(["remove", "seg"]) == 0
    assert json.loads(scopes[Scope.PROJECT].read_text())["mcpServers"] == {}


def test_add_refuses_a_duplicate_and_a_bad_entry_before_writing(scopes, capsys):
    assert mcp_cli(["add", "seg", "--", "seg-mcp"]) == 0
    assert mcp_cli(["add", "seg", "--", "other"]) == 2
    assert "already in" in capsys.readouterr().err

    assert mcp_cli(["add", "web", "--transport", "http"]) == 2
    # Caught by the same parser the server uses, before anything is written.
    assert (
        mcp_cli(
            ["add", "web", "--transport", "http", "--url", "http://x/mcp", "--sandbox"]
        )
        == 2
    )
    assert set(json.loads(scopes[Scope.LOCAL].read_text())["mcpServers"]) == {"seg"}


def test_remove_asks_for_a_scope_when_the_name_is_in_two(scopes, capsys):
    register(scopes[Scope.USER], {"seg": {"command": "a"}})
    register(scopes[Scope.LOCAL], {"seg": {"command": "b"}})

    assert mcp_cli(["remove", "seg"]) == 2
    assert "pass --scope" in capsys.readouterr().err
    assert mcp_cli(["remove", "seg", "--scope", "user"]) == 0
