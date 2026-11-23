"""The tool loop, and payloads kept out of the model's context."""

from __future__ import annotations

import asyncio
import base64
import json

from meddataos.core import workspace
from meddataos.core.loop import ToolLoop
from meddataos.core.model import ModelError


class ScriptedModel:
    """Answers with the next scripted message; raises when the script says so."""

    url = "scripted"

    def __init__(self, script):
        self.script = list(script)
        self.seen: list[tuple[list[dict], list[dict], dict | None]] = []

    def complete(self, messages, tools, response_format=None):
        self.seen.append((list(messages), list(tools), response_format))
        step = self.script.pop(0)

        if isinstance(step, Exception):
            raise step

        return step


class EchoLoop(ToolLoop):
    """Runs every tool call by echoing its name."""

    async def run_calls(self, calls):
        return [{"ran": call["function"]["name"]} for call in calls]


def tool_call(name: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": f"id_{name}", "function": {"name": name, "arguments": "{}"}}
        ],
    }


def test_tool_results_go_back_until_the_model_answers():
    model = ScriptedModel(
        [tool_call("fetch"), {"role": "assistant", "content": "Done."}]
    )
    loop = EchoLoop([{"role": "user", "content": "Go"}], model, [{"type": "function"}])

    assert asyncio.run(loop.reply()) == "Done."
    assert loop.messages[2] == {
        "role": "tool",
        "tool_call_id": "id_fetch",
        "content": json.dumps({"ran": "fetch"}),
    }


def test_a_refused_schema_is_dropped_before_the_tools_are():
    refused = ModelError("bad request", 400)
    model = ScriptedModel(
        [refused, refused, {"role": "assistant", "content": "Plain."}]
    )
    loop = EchoLoop(
        [{"role": "user", "content": "Go"}],
        model,
        [{"type": "function"}],
        answer_format=lambda: {"type": "json_schema"},
    )

    assert asyncio.run(loop.reply()) == "Plain."
    # First with schema and tools, then tools only, then neither.
    assert [(bool(tools), bool(schema)) for _, tools, schema in model.seen] == [
        (True, True),
        (True, False),
        (False, False),
    ]


def test_a_loop_that_never_answers_stops_at_its_step_budget():
    model = ScriptedModel([tool_call("fetch")] * 3)
    loop = EchoLoop([{"role": "user", "content": "Go"}], model, [], max_steps=3)

    try:
        asyncio.run(loop.reply())
    except ModelError as error:
        assert "no answer after 3 steps" in str(error)
    else:
        raise AssertionError("the loop should have stopped")


def test_a_payload_is_saved_once_and_replaced_by_its_path(tmp_path):
    volume = b"\x1f\x8b fake nifti"
    result = {
        "status": "ok",
        "result": {
            "data": {
                "volume_base64": base64.b64encode(volume).decode(),
                "shape": [2, 2],
            }
        },
    }

    first = workspace.offload_payloads(result, tmp_path)
    second = workspace.offload_payloads(result, tmp_path)
    data = first["result"]["data"]

    assert "volume_base64" not in data and data["shape"] == [2, 2]
    assert data["workspace_path"].startswith("volumes/") and data[
        "workspace_bytes"
    ] == len(volume)
    assert (tmp_path / data["workspace_path"]).read_bytes() == volume
    assert second == first
    assert len(list((tmp_path / "volumes").iterdir())) == 1


def test_an_undecodable_payload_is_removed(tmp_path):
    cleaned = workspace.offload_payloads({"image_base64": "not base64!"}, tmp_path)

    assert cleaned == {"image_base64": "<undecodable base64 payload removed>"}


def test_a_session_id_cannot_leave_the_workspaces_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACES", tmp_path)

    folder = workspace.workspace_for("../../etc")

    assert folder.parent == tmp_path


def test_each_turns_usage_is_reported():
    class CountingModel(ScriptedModel):
        def complete(self, messages, tools, response_format=None):
            message = super().complete(messages, tools, response_format)
            self.last_usage = {"input": 100, "output": 5, "cached": 0}
            return message

    class CountingLoop(EchoLoop):
        seen: list = []

        def record_usage(self, usage):
            CountingLoop.seen.append(usage)

    model = CountingModel(
        [tool_call("fetch"), {"role": "assistant", "content": "Done."}]
    )
    asyncio.run(CountingLoop([{"role": "user", "content": "Go"}], model, []).reply())

    assert len(CountingLoop.seen) == 2


def test_a_harness_model_that_refuses_tools_fails_with_the_fix():
    refused = ModelError(
        "Function tools are not supported in /v1/chat/completions", 400
    )
    model = ScriptedModel([refused])
    loop = EchoLoop(
        [{"role": "user", "content": "Go"}],
        model,
        [{"type": "function"}],
        tools_required=True,
    )

    try:
        asyncio.run(loop.reply())
    except ModelError as error:
        assert "cannot read the case" in str(error)
        assert "HARNESS_MODEL_API=responses" in str(error)
        assert error.status == 400
    else:
        raise AssertionError("a harness model without tools must not answer")


def test_a_volume_datactrl_wrote_to_the_workspace_keeps_only_its_relative_path(
    tmp_path,
):
    folder = tmp_path / "ws"
    (folder / "volumes").mkdir(parents=True)
    volume = folder / "volumes" / "abc.nii.gz"
    volume.write_bytes(b"\x1f\x8b nifti")
    outside = tmp_path / "elsewhere.nii.gz"
    outside.write_bytes(b"x")

    inside = workspace.offload_payloads({"data": {"volume_path": str(volume)}}, folder)
    escaped = workspace.offload_payloads(
        {"data": {"volume_path": str(outside)}}, folder
    )

    assert inside["data"] == {
        "workspace_path": "volumes/abc.nii.gz",
        "workspace_bytes": 8,
    }
    assert "removed" in escaped["data"]["workspace_path"]
