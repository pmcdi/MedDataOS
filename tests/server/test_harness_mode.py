"""The server with --harness: it talks to the model itself, over the Responses API.

A fake Responses endpoint stands in for the model. It reads the note, then
answers with a citation of that read. Nothing calls a real model.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_server import (
    _cite_handle,
    case_folder,  # noqa: F401 - the fixture, shared with test_server
    cited_handle,
    running_server,
    send_chat,
    wait_for_answer,
)


def as_chat(items: list[dict]) -> list[dict]:
    """Responses input items back in the chat shape the citation helpers read."""
    return [
        {"role": "tool", "content": item["output"]}
        for item in items
        if item.get("type") == "function_call_output"
    ]


def output_message(text: str) -> dict:
    return {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]}
        ],
        "usage": {"input_tokens": 1000, "output_tokens": 50},
    }


class FakeResponses(BaseHTTPRequestHandler):
    """A Responses endpoint: read the note, then cite what came back."""

    requests: list[dict] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeResponses.requests.append(body)
        payload = self.respond(body)
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def respond(self, body: dict) -> dict:
        instructions = body.get("instructions", "")

        if instructions.startswith("You fill in the patient card"):
            return output_message("{}")

        items = body["input"]
        resources = json.loads(
            instructions.split("Case resources: ", 1)[1].split("\n", 1)[0]
        )
        note = next(item for item in resources if item["modality"] == "note")

        if not any(item.get("type") == "function_call_output" for item in items):
            arguments = json.dumps({"resource_id": note["id"], "view": "text"})
            return {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "fetch",
                        "arguments": arguments,
                    }
                ]
            }

        messages = as_chat(items)
        return output_message(cited_handle(_cite_handle(messages), messages))

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def harness_server(case_folder):
    FakeResponses.requests = []
    model = ThreadingHTTPServer(("127.0.0.1", 0), FakeResponses)
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
            # Empty, not unset: the server's .env must not switch this to Azure.
            AZURE_OPENAI_ENDPOINT="",
            AZURE_OPENAI_REASONING_EFFORT="",
            OPENAI_BASE_URL=f"http://127.0.0.1:{model.server_address[1]}/v1",
            OPENAI_MODEL="fake-model",
            OPENAI_REASONING_EFFORT="low",
        ) as running:
            yield running
    finally:
        model.shutdown()


def asked_question() -> list[dict]:
    """The requests for the chat question, not the patient card's."""
    return [
        body
        for body in FakeResponses.requests
        if body["input"][0].get("content") == "What does the note say?"
    ]


@pytest.fixture(scope="module")
def answered(harness_server):
    session_id = send_chat(harness_server, "What does the note say?")["session_id"]
    return wait_for_answer(harness_server, session_id, timeout=30)["history"][-1]


def test_the_harness_answers_with_a_checked_citation(answered):
    answer = json.loads(answered["content"])

    assert answer["response"] == "The note records fatigue [cite:c1]."
    assert answer["citations"][0]["web_path"].endswith("clinic_note.txt")


def test_the_model_was_asked_over_the_responses_api(answered):
    questions = asked_question()
    first, second = questions[0], questions[-1]

    assert first["model"] == "fake-model" and first["store"] is False
    assert first["reasoning"] == {"effort": "low"}
    assert {tool["name"] for tool in first["tools"]} >= {"describe", "fetch"}
    # Once a file is read, the answer has to follow the citation schema.
    assert "text" not in first
    assert second["text"]["format"]["type"] == "json_schema"


def test_the_harness_offers_its_own_tools_and_lists_the_skills(answered):
    first = asked_question()[0]

    assert {"reference_search", "reference_read", "load_skill"} <= {
        tool["name"] for tool in first["tools"]
    }
    assert "Skills: dicom:" in first["instructions"]
    assert "never patient details" in first["instructions"]
    assert "Launch an agent when it has tools you do not" in first["instructions"]
    assert "follow what it says" not in first["instructions"]
    assert "would fill this conversation" not in first["instructions"]


def test_the_answer_carries_the_tokens_it_took(answered):
    # Two turns (fetch, then answer) but only the answer reported usage.
    assert answered["usage"] == {
        "router": {"calls": 1, "input": 1000, "output": 50, "cached": 0}
    }


def test_the_harness_is_the_default_and_both_flags_are_refused(case_folder):
    import subprocess
    import sys

    both = subprocess.run(
        [
            sys.executable,
            "-m",
            "meddataos.server",
            str(case_folder),
            "--harness",
            "--agent-url",
            "http://127.0.0.1:9/v1",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert both.returncode == 2 and "not both" in both.stderr

    # No flag: the server comes up in harness mode. Nothing is asked, so no
    # model is needed; the ledger is off so nothing calls one at startup.
    from test_server import get

    settings = {
        "HARNESS_LEDGER": "off",
        "HARNESS_SANDBOX": "off",
        "AZURE_OPENAI_ENDPOINT": "",
        "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
    }
    with running_server(case_folder, harness=None, **settings) as server:
        assert get(server, "/api/health")["status"] == "healthy"
