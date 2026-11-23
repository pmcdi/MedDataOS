"""--harness with an MCP server: the router measures, then cites what came back.

The server is the fake segmentation server from tests/harness/fixtures,
registered in a user-scope mcp.json that HARNESS_MCP_USER points at. A fake Responses endpoint plays
the model. It answers once with a volume the server never returned, then with
the one it did, so the verifier's refusal and the accepted answer are both seen.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_harness_mode import output_message
from test_server import (
    case_folder,  # noqa: F401 - the fixture, shared with test_server
    get,
    running_server,
    send_chat,
    wait_for_answer,
)

FAKE_SERVER = Path(__file__).parents[1] / "harness" / "fixtures" / "fake_mcp_server.py"
TOOL = "mcp__seg__measure"


def volume_answer(token: str, value: float) -> str:
    return json.dumps(
        {
            "response": f"The GTVp volume is {value} mL [cite:{token}].",
            "citations": [
                {
                    "kind": "derived",
                    "cite": token,
                    "method": "Measured by the segmentation server.",
                    "summary": "GTVp volume.",
                    "result": {"value": value, "unit": "mL"},
                    "inputs": [{"cite": token, "label": "GTVp measurement"}],
                }
            ],
        }
    )


class FakeModel(BaseHTTPRequestHandler):
    requests: list[dict] = []
    answers = 0

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        data = json.dumps(self.respond(body)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def respond(self, body: dict) -> dict:
        if body.get("instructions", "").startswith("You fill in the patient card"):
            return output_message("{}")

        FakeModel.requests.append(body)
        outputs = [
            item for item in body["input"] if item.get("type") == "function_call_output"
        ]

        if not outputs:
            arguments = json.dumps({"label": "GTVp"})
            return {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "m1",
                        "name": TOOL,
                        "arguments": arguments,
                    }
                ]
            }

        token = json.loads(outputs[0]["output"])["cite"]
        FakeModel.answers += 1

        # First a value the server never returned; the verifier sends it back.
        value = 2.6 if FakeModel.answers == 1 else 2.25
        return output_message(volume_answer(token, value))

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def answered(case_folder, tmp_path_factory):  # noqa: F811 - the imported fixture
    # Registered in the user scope, as `pixi run mcp add --scope user` would.
    registration = tmp_path_factory.mktemp("mcp") / "mcp.json"
    registration.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "seg": {
                        "command": sys.executable,
                        "args": [str(FAKE_SERVER)],
                        "meddataos": {
                            "evidence": "derived",
                            "router": True,
                            "timeout": 30,
                        },
                    }
                }
            }
        )
    )
    model = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
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
            # Servers connect in the background; ask once seg is connected.
            for _ in range(100):
                if (
                    get(server, "/api/health")["mcp"]
                    .get("seg", "")
                    .startswith("connected")
                ):
                    break
                time.sleep(0.1)

            session_id = send_chat(server, "How big is the GTVp?")["session_id"]
            yield wait_for_answer(server, session_id, timeout=60)["history"][-1]
    finally:
        model.shutdown()


def test_the_router_was_offered_the_servers_tools(answered):
    offered = {tool["name"] for tool in FakeModel.requests[0]["tools"]}

    assert {TOOL, "mcp__seg__segment"} <= offered
    # The citing rule names the server's tools as derived evidence.
    assert TOOL in FakeModel.requests[0]["instructions"]


def test_a_value_the_server_returned_is_cited_as_derived(answered):
    answer = json.loads(answered["content"])
    (citation,) = answer["citations"]

    assert answer["response"].startswith("The GTVp volume is 2.25 mL")
    assert citation["kind"] == "derived"
    assert citation["result"]["value"] == 2.25


def test_a_value_the_server_never_returned_was_sent_back(answered):
    assert FakeModel.answers == 2


def test_the_trace_shows_the_server_call(answered):
    step = next(step for step in answered["steps"] if step.get("name") == TOOL)

    assert step["label"] == "Running measure on seg"
    assert step["status"] == "done"
    assert "GTVp volume: 2.25 mL" in step.get("detail", "")
