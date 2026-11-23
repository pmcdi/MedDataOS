"""The sandbox: its docker command, when it refuses to run, and images it shows.

No container is started here; the command is checked as built.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest
from meddataos.core.loop import ToolLoop
from meddataos.core.model import to_responses_input
from meddataos.harness import sandbox
from meddataos.harness.sandbox import SandboxSettings
from meddataos.harness.tools import HarnessTools

PNG = b"\x89PNG\r\n\x1a\n fake"


class StubReferences:
    sites: list = []


def settings(
    tmp_path: Path, backend: str = "docker", runtime: str = "runsc"
) -> SandboxSettings:
    python_env = tmp_path / "env"
    (python_env / "bin").mkdir(parents=True, exist_ok=True)
    (python_env / "bin" / "python").write_text("")
    return SandboxSettings(
        backend=backend,
        runtime=runtime,
        image="debian:bookworm-slim",
        python_env=python_env,
        memory="4g",
        cpus="2",
        timeout=30,
    )


def test_the_container_has_no_network_and_reads_the_case_only(tmp_path):
    workspace, case = tmp_path / "ws", tmp_path / "case"
    command = sandbox.docker_command(settings(tmp_path), workspace, case, "python -V")

    for flag in (
        "--runtime=runsc",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        f"--volume={workspace.resolve()}:/workspace:rw",
        f"--volume={case.resolve()}:/case:ro",
    ):
        assert flag in command

    environment = (tmp_path / "env").resolve()
    assert f"--volume={environment}:{environment}:ro" in command
    assert command[-8:] == [
        "debian:bookworm-slim",
        "timeout",
        "-s",
        "KILL",
        "30",
        "/bin/sh",
        "-c",
        "python -V",
    ]


def test_a_named_container_is_removed_when_the_host_stops_waiting(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sandbox, "unavailable_reason", lambda settings: None)
    ran = []

    def fake_run(arguments, **options):
        ran.append(arguments)
        if arguments[1] == "run":
            raise sandbox.subprocess.TimeoutExpired(arguments, options["timeout"])
        return sandbox.subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    result = sandbox.run_command(settings(tmp_path), tmp_path, None, "sleep 999")

    name = next(flag.split("=", 1)[1] for flag in ran[0] if flag.startswith("--name="))
    assert name.startswith("meddataos-exec-")
    assert ran[1] == ["docker", "rm", "--force", name]
    assert "ran past 30 s" in result["error"]


def test_a_kill_after_the_full_time_limit_reads_as_a_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "unavailable_reason", lambda settings: None)
    clock = iter([0.0, 31.0, 100.0, 101.0])
    monkeypatch.setattr(sandbox.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        sandbox.subprocess,
        "run",
        lambda arguments, **options: sandbox.subprocess.CompletedProcess(
            arguments, 137, "", ""
        ),
    )

    timed_out = sandbox.run_command(settings(tmp_path), tmp_path, None, "sleep 999")
    # The same exit code one second in is an out-of-memory kill, shown as is.
    oom = sandbox.run_command(settings(tmp_path), tmp_path, None, "python big.py")

    assert "ran past 30 s" in timed_out["error"]
    assert oom["exit_code"] == 137


def test_it_refuses_rather_than_fall_back_to_a_weaker_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        sandbox, "docker_runtimes", lambda: {"runc", "io.containerd.runc.v2"}
    )

    problem = sandbox.unavailable_reason(settings(tmp_path))
    named = sandbox.unavailable_reason(settings(tmp_path, runtime="runc"))

    assert (
        "no 'runsc' runtime" in problem
        and "lists: io.containerd.runc.v2, runc" in problem
    )
    assert named is None
    assert "off" in sandbox.unavailable_reason(settings(tmp_path, backend="off"))
    assert sandbox.run_command(settings(tmp_path), tmp_path, None, "ls") == {
        "error": problem
    }


def test_long_output_keeps_its_end():
    trimmed = sandbox._trim("x" * 9000 + "RESULT")

    assert trimmed.endswith("RESULT") and trimmed.startswith("[... 1006 characters cut")


def test_only_images_inside_the_workspace_can_be_shown(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "out").mkdir(parents=True)
    (workspace / "out" / "slice.png").write_bytes(PNG)
    (workspace / "out" / "notes.txt").write_text("text")
    (tmp_path / "secret.png").write_bytes(PNG)

    shown = sandbox.image_for_model(workspace, "out/slice.png")

    assert (
        base64.b64decode(shown["image_base64"]) == PNG
        and shown["mime_type"] == "image/png"
    )
    assert (
        "not a file in the workspace"
        in sandbox.image_for_model(workspace, "../secret.png")["error"]
    )
    assert (
        "not a PNG or JPEG"
        in sandbox.image_for_model(workspace, "out/notes.txt")["error"]
    )


@pytest.fixture
def ready_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(sandbox, "docker_runtimes", lambda: {"runsc"})
    return HarnessTools(
        skills_dir=tmp_path / "no-skills",
        references=StubReferences(),
        sandbox=settings(tmp_path),
    )


def test_exec_and_view_image_are_offered_only_when_the_sandbox_can_run(
    tmp_path, ready_tools
):
    off = HarnessTools(
        skills_dir=tmp_path / "no-skills",
        references=StubReferences(),
        sandbox=settings(tmp_path, backend="off"),
    )

    assert {"exec", "view_image"} <= ready_tools.names
    assert not {"exec", "view_image"} & off.names
    assert off.run("exec", {"command": "ls"}, tmp_path)["status"] == "error"


def test_exec_runs_in_the_session_workspace(tmp_path, ready_tools, monkeypatch):
    seen = {}

    def fake_run(settings, workspace, case_root, command):
        seen.update(workspace=workspace, case_root=case_root, command=command)
        return {"exit_code": 0, "stdout": "2.25\n", "stderr": ""}

    monkeypatch.setattr("meddataos.harness.tools.run_command", fake_run)
    result = ready_tools.run(
        "exec", {"command": "python out/volume.py"}, tmp_path / "ws", tmp_path / "case"
    )

    assert result["result"]["stdout"] == "2.25\n"
    assert seen == {
        "workspace": tmp_path / "ws",
        "case_root": tmp_path / "case",
        "command": "python out/volume.py",
    }


def test_view_image_attaches_the_picture_and_the_loop_sends_it_as_a_user_turn(
    tmp_path, ready_tools
):
    workspace = tmp_path / "ws"
    (workspace / "previews").mkdir(parents=True)
    (workspace / "previews" / "a.png").write_bytes(PNG)
    result = ready_tools.run("view_image", {"path": "previews/a.png"}, workspace)

    assert result["result"] == {"path": "previews/a.png", "shown": True}
    assert result["attach_image"]["mime_type"] == "image/png"

    from meddataos.server.dispatch import Conversation

    follow = Conversation.follow_up(None, [result])  # type: ignore[arg-type]
    instructions, items = to_responses_input(follow)

    assert follow[0]["role"] == "user"
    assert items[0]["content"][0] == {
        "type": "input_text",
        "text": "view_image previews/a.png:",
    }
    assert items[0]["content"][1]["image_url"].startswith("data:image/png;base64,")


def test_the_loop_adds_follow_up_messages_after_tool_results():
    class Model:
        url = "scripted"

        def __init__(self):
            self.turns = [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c1", "function": {"name": "look"}}],
                },
                {"role": "assistant", "content": "I see it."},
            ]

        def complete(self, messages, tools, response_format=None):
            return self.turns.pop(0)

    class LookLoop(ToolLoop):
        async def run_calls(self, calls):
            return [{"status": "ok"}]

        def follow_up(self, results):
            return [{"role": "user", "content": "picture"}]

    loop = LookLoop([{"role": "user", "content": "Look"}], Model(), [])

    assert asyncio.run(loop.reply()) == "I see it."
    assert [message["role"] for message in loop.messages] == [
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
    ]
    assert json.loads(loop.messages[2]["content"]) == {"status": "ok"}
