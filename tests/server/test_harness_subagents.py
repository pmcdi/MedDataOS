"""--harness with a subagent: the router spawns the dicom skill, which reads the note.

One fake Responses endpoint plays both models. It tells them apart by their
instructions: the subagent's carry the skill's text. Nothing calls a real model.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_harness_mode import as_chat, output_message
from test_server import (
    _cite_handle,
    case_folder,  # noqa: F401 - the fixture, shared with test_server
    cited_handle,
    running_server,
    send_chat,
    wait_for_answer,
)

QUESTION = "Ask the dicom subagent what the note says."
SKILL_MARK = "# DICOM and radiology"


class FakeModels(BaseHTTPRequestHandler):
    router: list[dict] = []
    subagent: list[dict] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        data = json.dumps(self.respond(body)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def respond(self, body: dict) -> dict:
        instructions = body.get("instructions", "")
        items = body["input"]
        done_tools = [
            item for item in items if item.get("type") == "function_call_output"
        ]

        if instructions.startswith("You fill in the patient card"):
            return output_message("{}")

        if SKILL_MARK in instructions:
            FakeModels.subagent.append(body)
            resources = json.loads(
                instructions.split("Case resources: ", 1)[1].split("\n", 1)[0]
            )
            note = next(item for item in resources if item["modality"] == "note")

            if not done_tools:
                arguments = json.dumps({"resource_id": note["id"], "view": "text"})
                return {
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "s1",
                            "name": "fetch",
                            "arguments": arguments,
                        }
                    ]
                }

            messages = as_chat(items)
            return output_message(cited_handle(_cite_handle(messages), messages))

        FakeModels.router.append(body)

        if not done_tools:
            arguments = json.dumps(
                {"agent": "dicom", "task": "What does the note say?"}
            )
            return {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "r1",
                        "name": "spawn_subagent",
                        "arguments": arguments,
                    }
                ]
            }

        reply = json.loads(done_tools[-1]["output"])
        finding = reply["findings"][0]["id"]
        return output_message(
            json.dumps(
                {
                    "response": f"The note records fatigue [cite:{finding}].",
                    "citations": [],
                }
            )
        )

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def answered(case_folder):  # noqa: F811 - the imported fixture
    model = ThreadingHTTPServer(("127.0.0.1", 0), FakeModels)
    threading.Thread(target=model.serve_forever, daemon=True).start()

    try:
        with running_server(
            case_folder,
            harness=True,
            # Building the ledger would call the notes model, and the sandbox
            # would start containers; a developer's .env must not turn either on.
            HARNESS_LEDGER="off",
            HARNESS_SANDBOX="off",
            HARNESS_MODEL_API="responses",
            AZURE_OPENAI_ENDPOINT="",
            AZURE_OPENAI_REASONING_EFFORT="",
            OPENAI_BASE_URL=f"http://127.0.0.1:{model.server_address[1]}/v1",
            OPENAI_MODEL="fake-model",
        ) as server:
            session_id = send_chat(server, QUESTION)["session_id"]
            yield wait_for_answer(server, session_id, timeout=30)
    finally:
        model.shutdown()


def test_the_router_cites_the_subagents_checked_finding(answered):
    final = answered["history"][-1]
    answer = json.loads(final["content"])

    assert answer["response"] == "The note records fatigue [cite:dicom.1]."
    assert answer["citations"][0]["web_path"].endswith("clinic_note.txt")
    assert any(
        step.get("kind") == "subagent" and step.get("name") == "dicom"
        for step in final["steps"]
    )
    # The subagent's own read is tagged with its name, for the page to nest it.
    assert any(
        step.get("name") == "fetch" and step.get("by") == "dicom"
        for step in final["steps"]
    )


def test_the_subagent_got_only_its_skills_tools(answered):
    router_tools = {tool["name"] for tool in FakeModels.router[0]["tools"]}
    subagent_tools = {tool["name"] for tool in FakeModels.subagent[0]["tools"]}

    assert "spawn_subagent" in router_tools
    assert subagent_tools <= {
        "dicom_tree",
        "dicom_groups",
        "links",
        "assert_link",
        "describe",
        "fetch",
        "reference_search",
        "reference_read",
    }
    assert {"fetch", "reference_search"} <= subagent_tools
    assert (
        not {"spawn_subagent", "load_skill", "discover", "notes_search"}
        & subagent_tools
    )


def test_the_server_starts_with_no_mcp_registrations(answered):
    spawn = next(
        tool
        for tool in FakeModels.router[0]["tools"]
        if tool["name"] == "spawn_subagent"
    )
    subagent_tools = {tool["name"] for tool in FakeModels.subagent[0]["tools"]}

    assert "dicom" in spawn["parameters"]["properties"]["agent"]["enum"]
    assert "mcp__" not in spawn["description"]
    assert subagent_tools
    assert not any(name.startswith("mcp__") for name in subagent_tools)
