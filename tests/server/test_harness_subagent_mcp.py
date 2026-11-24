"""--harness with an agent that calls an MCP tool only it is granted.

The fake server is registered as ``totalseg``, which the dicom agent grants and
the router is not offered. One fake Responses endpoint plays both models.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import pytest
from test_harness_mcp import FAKE_SERVER, volume_answer
from test_harness_mcp import FakeModel as RouterModel
from test_harness_mode import output_message
from test_server import (
    case_folder,  # noqa: F401 - the fixture, shared with test_server
    get,
    running_server,
    send_chat,
    wait_for_answer,
)

TOOL = "mcp__totalseg__measure"
SKILL_MARK = "# DICOM and radiology"


def function_call(name: str, arguments: dict) -> dict:
    return {
        "output": [
            {
                "type": "function_call",
                "call_id": f"call-{name}",
                "name": name,
                "arguments": json.dumps(arguments),
            }
        ]
    }


class FakeModels(RouterModel):
    router: list[dict] = []
    subagent: list[dict] = []

    def respond(self, body: dict) -> dict:
        instructions = body.get("instructions", "")
        outputs = [
            json.loads(item["output"])
            for item in body["input"]
            if item.get("type") == "function_call_output"
        ]

        if instructions.startswith("You fill in the patient card"):
            return output_message("{}")

        if SKILL_MARK in instructions:
            FakeModels.subagent.append(body)

            if not outputs:
                return function_call(TOOL, {"label": "GTVp"})

            return output_message(volume_answer(outputs[0]["cite"], 2.25))

        FakeModels.router.append(body)

        if not outputs:
            return function_call(
                "spawn_subagent", {"agent": "dicom", "task": "Measure the GTVp."}
            )

        finding = outputs[-1]["findings"][0]["id"]
        return output_message(
            json.dumps(
                {
                    "response": f"The GTVp volume is 2.25 mL [cite:{finding}].",
                    "citations": [],
                }
            )
        )


@pytest.fixture(scope="module")
def answered(case_folder, tmp_path_factory):  # noqa: F811 - the imported fixture
    registration = tmp_path_factory.mktemp("mcp") / "mcp.json"
    registration.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "totalseg": {
                        "command": sys.executable,
                        "args": [str(FAKE_SERVER)],
                        "meddataos": {"evidence": "derived", "timeout": 30},
                    }
                }
            }
        )
    )
    model = ThreadingHTTPServer(("127.0.0.1", 0), FakeModels)
    threading.Thread(target=model.serve_forever, daemon=True).start()

    try:
        with running_server(
            case_folder,
            harness=True,
            HARNESS_MCP_USER=str(registration),
            HARNESS_LEDGER="off",
            HARNESS_SANDBOX="off",
            HARNESS_MODEL_API="responses",
            AZURE_OPENAI_ENDPOINT="",
            AZURE_OPENAI_REASONING_EFFORT="",
            OPENAI_BASE_URL=f"http://127.0.0.1:{model.server_address[1]}/v1",
            OPENAI_MODEL="fake-model",
        ) as server:
            # Servers connect in the background; ask once this one is connected.
            for _ in range(100):
                status = get(server, "/api/health")["mcp"].get("totalseg", "")

                if status.startswith("connected"):
                    break

                time.sleep(0.1)

            session_id = send_chat(server, "How big is the GTVp?")["session_id"]
            yield wait_for_answer(server, session_id, timeout=60)["history"][-1]
    finally:
        model.shutdown()


def test_only_the_agent_was_offered_the_tool(answered):
    assert TOOL not in {tool["name"] for tool in FakeModels.router[0]["tools"]}
    assert TOOL in {tool["name"] for tool in FakeModels.subagent[0]["tools"]}


def test_the_agents_call_reached_the_mcp_server(answered):
    step = next(step for step in answered["steps"] if step.get("name") == TOOL)

    assert step["status"] == "done", step.get("detail")
    assert step["by"] == "dicom"
    assert "GTVp volume: 2.25 mL" in step.get("detail", "")


def test_the_router_cites_the_value_the_agent_measured(answered):
    answer = json.loads(answered["content"])

    assert answer["response"] == "The GTVp volume is 2.25 mL [cite:dicom.1]."
    assert answer["citations"][0]["kind"] == "derived"
    assert answer["citations"][0]["result"]["value"] == 2.25
