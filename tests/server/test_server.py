"""Run the real server on a small case folder and talk to it like the page and an agent."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from websockets.sync.client import connect

APP = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def case_folder(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("cases") / "P0042"
    folder.mkdir()
    (folder / "clinic_note.txt").write_text(
        "Chief complaint: fatigue.\nPlan: follow-up in 2 weeks.\n"
    )
    (folder / "labs.csv").write_text("test,value\nhemoglobin,13.1\nwbc,6.2\n")
    return folder


@contextmanager
def running_server(
    case_folder,
    agent_url="http://127.0.0.1:9/v1",
    harness=False,
    scratch=None,
    **settings,
):
    port = _free_port()
    env = {key: value for key, value in os.environ.items() if key != "AGENT_URL"}
    # Chats and pins are stored beside the case folder, inside pytest's own
    # temporary tree, so nothing from an earlier run is found again.
    # ``scratch`` keeps the store, cache and config out of a real case's folder.
    scratch = Path(scratch) if scratch is not None else Path(case_folder).parent
    store = str(scratch / "store")
    # No developer config: configs/harness.toml would start real MCP servers.
    empty_config = scratch / "harness.toml"
    empty_config.touch()
    # MCP registrations stay empty: these files are not created. A grant to a
    # server that is not registered is a startup warning, not a failure.
    # A test that passes HARNESS_MCP_PROJECT replaces the path.
    env.update(
        HOST="127.0.0.1",
        PORT=str(port),
        MEDDATAOS_STORE=store,
        HARNESS_CONFIG=str(empty_config),
        HARNESS_CACHE=str(scratch / "cache"),
        # Not the developer's registrations: configs/mcp.json and ~/.meddataos.
        HARNESS_MCP_USER=str(scratch / "mcp-user.json"),
        HARNESS_MCP_PROJECT=str(scratch / "mcp-project.json"),
        HARNESS_MCP_LOCAL=str(scratch / "mcp-local.json"),
    )
    env.update(settings)
    command = [sys.executable, "-m", "meddataos.server", str(case_folder)]
    # True passes --harness, None passes no flag (the harness is the default),
    # and False points the server at the agent URL.
    if harness:
        command.append("--harness")
    elif harness is False:
        command.extend(["--agent-url", agent_url])

    process = subprocess.Popen(
        command,
        cwd=APP,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"

    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("server did not start")

        yield {"http": base, "ws": f"ws://127.0.0.1:{port}"}

    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(scope="module")
def server(case_folder):
    with running_server(case_folder) as running:
        yield running


def get(server, path):
    with urllib.request.urlopen(server["http"] + path) as response:
        return json.load(response)


def post(server, path, body):
    request = urllib.request.Request(
        server["http"] + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )

    with urllib.request.urlopen(request) as response:
        return json.load(response)


def send_chat(server, message, patient_id="P0042"):
    form = urllib.parse.urlencode(
        {"message": message, "patient_id": patient_id}
    ).encode()

    with urllib.request.urlopen(server["http"] + "/api/chat", data=form) as response:
        return json.load(response)


def wait_for_answer(server, session_id, timeout=10):
    deadline = time.time() + timeout

    while time.time() < deadline:
        session = get(server, f"/api/session/{session_id}")

        if not session["processing"]:
            return session

        time.sleep(0.1)

    raise AssertionError("no answer arrived")


def test_page_is_served(server):
    with urllib.request.urlopen(server["http"] + "/") as response:
        assert response.status == 200
        assert b"MedDataOS" in response.read()


def test_a_case_without_dicom_has_no_previews(server):
    assert get(server, "/api/dicom/previews") == []
    request = urllib.request.Request(
        server["http"] + "/api/dicom/preview?web_path=/case/clinic_note.txt"
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request)
    assert caught.value.code == 404


def test_patient_card_comes_from_the_folder(server):
    patients = get(server, "/api/patients")

    assert [patient["id"] for patient in patients] == ["P0042"]
    assert [item["label"] for item in patients[0]["modalities"]] == ["Notes", "Tables"]


def patch(server, path, body):
    request = urllib.request.Request(
        server["http"] + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )

    with urllib.request.urlopen(request) as response:
        return json.load(response)


def test_chats_pins_and_reviews_survive_a_restart(tmp_path):
    folder = tmp_path / "P0099"
    folder.mkdir()
    (folder / "clinic_note.txt").write_text("Chief complaint: fatigue.\n")

    agent = ThreadingHTTPServer(("127.0.0.1", 0), FakeAgent)
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{agent.server_address[1]}/v1"

    try:
        with running_server(folder, agent_url=url, AGENT_MODEL="fake") as first:
            question = "Keep this chat after restart."
            session_id = send_chat(first, question, patient_id="P0099")["session_id"]
            answer = wait_for_answer(first, session_id)
            assert answer["history"][-1]["content"] == "Plain answer."

            pinned = post(
                first,
                "/api/patients/P0099/pins",
                {
                    "type": "text",
                    "text": "follow the fatigue plan",
                    "source": session_id,
                },
            )
            assert pinned["status"] == "ok"

            reviewed = patch(
                first,
                "/api/patients/P0099/reviews",
                {"key": "/case/clinic_note.txt", "checklist_state": {"0": True}},
            )
            assert reviewed["key"] == "/case/clinic_note.txt"

        with running_server(folder, agent_url=url, AGENT_MODEL="fake") as second:
            chats = get(second, "/api/patients/P0099/conversations")
            assert [item["session_id"] for item in chats] == [session_id]
            assert chats[0]["preview"] == question

            restored = get(second, f"/api/session/{session_id}")
            assert restored["processing"] is False
            assert restored["history"][-1]["content"] == "Plain answer."
            assert get(second, "/api/patients/P0099/pins")[0]["text"] == (
                "follow the fatigue plan"
            )
            assert get(second, "/api/patients/P0099/reviews") == {
                "/case/clinic_note.txt": {"0": True},
            }
    finally:
        agent.shutdown()


def test_case_files_are_served(server):
    with urllib.request.urlopen(server["http"] + "/case/clinic_note.txt") as response:
        assert b"fatigue" in response.read()


def test_example_agent_requires_a_model():
    env = {key: value for key, value in os.environ.items() if key != "OPENAI_MODEL"}
    env["OPENAI_MODEL"] = ""

    for key in (
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
    ):
        env[key] = ""

    result = subprocess.run(
        [sys.executable, str(APP / "examples" / "agent.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "OPENAI_MODEL" in result.stderr


def test_example_agent_forwards_chat_completions():
    class RecordingModel(BaseHTTPRequestHandler):
        requests: list[dict] = []

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            RecordingModel.requests.append(body)
            data = json.dumps(
                {
                    "choices": [
                        {"message": {"role": "assistant", "content": "Plain answer."}}
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    RecordingModel.requests = []
    model = ThreadingHTTPServer(("127.0.0.1", 0), RecordingModel)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    port = _free_port()
    env = {
        **os.environ,
        "OPENAI_MODEL": "real-model",
        "OPENAI_BASE_URL": f"http://127.0.0.1:{model.server_address[1]}/v1",
        "OPENAI_API_KEY": "secret",
        "AZURE_OPENAI_API_KEY": "",
        "AZURE_OPENAI_ENDPOINT": "",
        "AZURE_OPENAI_DEPLOYMENT": "",
    }
    agent = subprocess.Popen(
        [sys.executable, str(APP / "examples" / "agent.py"), "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    example = {"http": f"http://127.0.0.1:{port}"}

    try:
        deadline = time.time() + 5
        while True:
            try:
                sent = post(
                    example,
                    "/v1/chat/completions",
                    {
                        "model": "example",
                        "messages": [{"role": "user", "content": "Say hello."}],
                        "tools": [{"type": "function", "function": {"name": "fetch"}}],
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {"name": "cited_answer"},
                        },
                    },
                )
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.05)

        assert sent["choices"][0]["message"]["content"] == "Plain answer."
        forwarded = RecordingModel.requests[-1]
        assert forwarded["model"] == "real-model"
        assert forwarded["tools"][0]["function"]["name"] == "fetch"
        assert forwarded["response_format"]["json_schema"]["name"] == "cited_answer"
    finally:
        agent.terminate()
        agent.wait(timeout=10)
        model.shutdown()


def cited_note(web_path: str) -> str:
    return json.dumps(
        {
            "response": "The note records fatigue [cite:1].",
            "citations": [
                {
                    "id": "1",
                    "file": "clinic_note.txt",
                    "web_path": web_path,
                    "summary": "Chief complaint: fatigue.",
                }
            ],
        }
    )


def _note_slice(messages: list[dict]) -> dict | None:
    """An exact slice of note text a tool result returned, for the fake agent to cite."""
    for item in reversed(messages):
        if item.get("role") != "tool":
            continue

        try:
            payload = json.loads(item["content"])
        except (TypeError, json.JSONDecodeError):
            continue

        result = payload.get("result") if isinstance(payload, dict) else None
        data = result.get("data") if isinstance(result, dict) else None
        text = data.get("text") if isinstance(data, dict) else None

        if not isinstance(text, str) or not text.strip():
            continue

        line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        start = text.find(line)

        if start < 0 or not line:
            continue

        return {
            "n": 1,
            "claim": "Chief complaint: fatigue.",
            "quote": line,
            "start": start,
            "end": start + len(line),
        }

    return None


def cited_handle(handle: str, messages: list[dict] | None = None) -> str:
    citation: dict = {
        "cite": handle,
        "summary": "Chief complaint: fatigue.",
    }
    evidence = _note_slice(messages or [])

    if evidence:
        citation["evidence"] = [evidence]

    return json.dumps(
        {
            "response": f"The note records fatigue [cite:{handle}].",
            "citations": [citation],
        }
    )


def _cite_handle(messages: list[dict]) -> str:
    for item in reversed(messages):
        if item.get("role") != "tool":
            continue

        try:
            payload = json.loads(item["content"])
        except json.JSONDecodeError:
            continue

        if isinstance(payload.get("cite"), str):
            return payload["cite"]

    return "c1"


CARD = {
    "name": "Jane Doe",
    "age": 64,
    "sex": "F",
    "blood_type": None,
    "allergies": ["Penicillin", 3],
    "conditions": ["Fatigue"],
    "data_dates": {"labs": "2026-01-02", "notes": "last week"},
}


class FakeAgent(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint. Tags in the question choose how it behaves."""

    requests: list[dict] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAgent.requests.append(body)
        status, payload = self.respond(body)
        data = json.dumps(payload).encode()

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def respond(self, body: dict) -> tuple[int, dict]:
        messages = body["messages"]
        question = next(item["content"] for item in messages if item["role"] == "user")
        system = messages[0]["content"]
        resources = json.loads(system.split("Case resources: ", 1)[1].split("\n", 1)[0])
        note = next(item for item in resources if item["modality"] == "note")
        has_read = any(item["role"] == "tool" for item in messages)
        corrected = any(
            item["role"] == "user" and item["content"].startswith("The server refused")
            for item in messages
        )

        if question == "Fill in the patient card.":
            return 200, self.message(content="```json\n" + json.dumps(CARD) + "\n```")

        if "[dump]" in question and not corrected:
            return 200, self.message(
                content=(
                    "The scan is a CT [cite:dicom:1.2.3].\n\n"
                    '{"response": "The scan is a CT [cite:dicom:1.2.3].", "citations":'
                )
            )

        if "[dump]" in question:
            return 200, self.message(content=cited_note(note["web_path"]))

        if "[echo]" in question and not corrected:
            return 200, self.message(
                content='{"when": "Plain text is fine.", "message": ""}'
            )

        if "[wrap]" in question:
            return 200, self.message(
                content='{"message": "Hello! How can I assist you today?"}'
            )

        if "[broken]" in question:
            return 200, {"oops": True}

        if "[bad args]" in question and not has_read:
            return 200, self.message(
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "fetch", "arguments": "{not json"},
                    }
                ]
            )

        if "[no tools]" in question and "tools" in body:
            return 400, {"error": {"message": "tools are not supported"}}

        if "[no schema]" in question and "response_format" in body:
            return 400, {"error": {"message": "response_format is not supported"}}

        if "[discover]" in question and not has_read:
            return 200, self.message(
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "discover",
                            "arguments": json.dumps({"root": "/tmp"}),
                        },
                    }
                ]
            )

        if "[read]" in question and not has_read:
            return 200, self.message(
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "fetch",
                            "arguments": json.dumps(
                                {"resource_id": note["id"], "view": "text"}
                            ),
                        },
                    }
                ]
            )

        if "[never]" in question or ("[forget]" in question and not corrected):
            return 200, self.message(content="Fatigue.")

        if "[alone]" in question and has_read and not corrected:
            handle = _cite_handle(messages)
            alone = json.dumps(
                {
                    "response": f"The note records fatigue. [cite:{handle}]",
                    "citations": [
                        {
                            "cite": handle,
                            "summary": "Chief complaint: fatigue.",
                        }
                    ],
                }
            )
            return 200, self.message(content=alone)

        if "[multi]" in question and has_read and not corrected:
            handle = _cite_handle(messages)
            return 200, self.message(
                content=json.dumps(
                    {
                        "response": (
                            f"The CT and the note both record fatigue [cite:{handle}] [cite:{handle}]."
                        ),
                        "citations": [
                            {
                                "cite": handle,
                                "summary": "Chief complaint: fatigue.",
                            }
                        ],
                    }
                )
            )

        if "[multi]" in question and has_read:
            handle = _cite_handle(messages)
            citation: dict = {
                "cite": handle,
                "summary": "Chief complaint: fatigue.",
            }
            evidence = _note_slice(messages)

            if evidence:
                citation["evidence"] = [evidence]

            return 200, self.message(
                content=json.dumps(
                    {
                        "response": (
                            f"The CT records fatigue [cite:{handle}]. The note records it too [cite:{handle}]."
                        ),
                        "citations": [citation],
                    }
                )
            )

        if "[discover]" in question and has_read:
            return 200, self.message(content="Discovered.")

        if has_read:
            return 200, self.message(
                content=f"```json\n{cited_handle(_cite_handle(messages), messages)}\n```"
            )

        return 200, self.message(content="Plain answer.")

    @staticmethod
    def message(**fields) -> dict:
        return {
            "choices": [{"message": {"role": "assistant", "content": None, **fields}}]
        }

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def dispatch_server(case_folder):
    agent = ThreadingHTTPServer(("127.0.0.1", 0), FakeAgent)
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{agent.server_address[1]}/v1"

    try:
        with running_server(case_folder, agent_url=url, AGENT_MODEL="fake") as running:
            yield running
    finally:
        agent.shutdown()


def requests_for(question: str) -> list[dict]:
    return [
        body
        for body in FakeAgent.requests
        if body["messages"][1]["content"] == question
    ]


def test_audit_run_records_tools_refusals_and_calls(dispatch_server):
    session_id = send_chat(
        dispatch_server,
        "[read][forget] What does the note say?",
    )["session_id"]
    wait_for_answer(dispatch_server, session_id)

    payload = get(dispatch_server, f"/api/session/{session_id}/audit")
    assert payload["status"] == "success"
    assert payload["runs"]
    run = payload["runs"][0]
    assert run["session_id"] == session_id
    assert "[read]" in run["question"]
    assert run["refusals"]
    assert any(step.get("kind") == "tool" for step in run["steps"])
    assert any(item.get("cite") == "c1" for item in run["calls"])


def test_audit_runs_survive_a_restart(tmp_path):
    folder = tmp_path / "P0043"
    folder.mkdir()
    (folder / "clinic_note.txt").write_text("Chief complaint: fatigue.\n")

    agent = ThreadingHTTPServer(("127.0.0.1", 0), FakeAgent)
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{agent.server_address[1]}/v1"

    try:
        with running_server(folder, agent_url=url, AGENT_MODEL="fake") as first:
            session_id = send_chat(first, "Audit me.", patient_id="P0043")["session_id"]
            wait_for_answer(first, session_id)
            run_id = get(first, f"/api/session/{session_id}/audit")["runs"][0]["run_id"]

        with running_server(folder, agent_url=url, AGENT_MODEL="fake") as second:
            runs = get(second, f"/api/session/{session_id}/audit")["runs"]
            assert runs[0]["run_id"] == run_id
            assert runs[0]["answer"] == "Plain answer."
    finally:
        agent.shutdown()


def test_server_sends_the_question_to_the_agent(dispatch_server):
    question = "Say hello."

    with connect(dispatch_server["ws"] + "/ws") as page:
        assert json.loads(page.recv(timeout=5))["type"] == "system"
        session_id = send_chat(dispatch_server, question)["session_id"]
        pushed = json.loads(page.recv(timeout=5))

        while pushed["type"] in {"patient", "status", "step"}:
            pushed = json.loads(page.recv(timeout=5))

    assert pushed["type"] == "assistant"
    assert pushed["session_id"] == session_id
    assert pushed["message"] == "Plain answer."

    sent = requests_for(question)
    assert len(sent) == 1
    assert sent[0]["model"] == "fake"

    for tool in sent[0]["tools"]:
        params = tool["function"]["parameters"]
        assert "root" not in params.get("properties", {})
        assert "case_id" not in params.get("properties", {})


def test_the_agent_fills_in_the_patient_card(dispatch_server):
    deadline = time.time() + 10

    while time.time() < deadline:
        card = get(dispatch_server, "/api/patients")[0]

        if card["name"] != "P0042":
            break

        time.sleep(0.1)

    assert card["id"] == "P0042"
    assert card["name"] == "Jane Doe"
    assert card["age"] == "64"
    assert card["allergies"] == ["Penicillin"]
    assert card["conditions"] == ["Fatigue"]
    assert card["data_dates"] == {"labs": "2026-01-02"}
    assert [item["label"] for item in card["modalities"]] == ["Notes", "Tables"]

    with connect(dispatch_server["ws"] + "/ws") as page:
        assert json.loads(page.recv(timeout=5))["type"] == "system"
        assert json.loads(page.recv(timeout=5))["patient"]["name"] == "Jane Doe"


def test_the_card_shows_the_folder_name_without_an_agent(server):
    card = get(server, "/api/patients")[0]

    assert card["name"] == "P0042"
    assert card["age"] == ""
    assert card["conditions"] == []


def test_server_reads_the_folder_it_opened(dispatch_server):
    question = "[discover] What is in the folder?"
    session_id = send_chat(dispatch_server, question)["session_id"]
    answer = wait_for_answer(dispatch_server, session_id)["history"][-1]

    assert answer["content"] == "Discovered."
    tool = next(
        item
        for item in requests_for(question)[-1]["messages"]
        if item["role"] == "tool"
    )
    assert json.loads(tool["content"])["result"]["root"].endswith("P0042")


def test_server_rejects_a_marker_that_stands_alone(dispatch_server):
    question = "[read][alone] What does the note say?"
    session_id = send_chat(dispatch_server, question)["session_id"]
    answer = json.loads(
        wait_for_answer(dispatch_server, session_id)["history"][-1]["content"]
    )

    assert answer["response"].count("[cite:c1]") == 1
    assert any(
        item["role"] == "user" and item["content"].startswith("The server refused")
        for item in requests_for(question)[-1]["messages"]
    )


def test_a_greeting_wrapped_in_json_is_shown_as_text(dispatch_server):
    session_id = send_chat(dispatch_server, "[wrap] Hello")["session_id"]
    answer = wait_for_answer(dispatch_server, session_id)["history"][-1]

    assert answer["content"] == "Hello! How can I assist you today?"


def test_json_without_files_is_sent_back_for_plain_text(dispatch_server):
    question = "[echo] Hello"
    session_id = send_chat(dispatch_server, question)["session_id"]
    answer = wait_for_answer(dispatch_server, session_id)["history"][-1]

    assert answer["content"] == "Plain answer."
    assert any(
        item["role"] == "user" and "plain text, not JSON" in item["content"]
        for item in requests_for(question)[-1]["messages"]
    )


def test_prose_followed_by_json_is_sent_back_for_a_citation_object(dispatch_server):
    question = "[dump] Have a look at its imaging"
    session_id = send_chat(dispatch_server, question)["session_id"]
    answer = json.loads(
        wait_for_answer(dispatch_server, session_id)["history"][-1]["content"]
    )

    assert answer["response"].count("[cite:1]") == 1
    assert any(
        item["role"] == "user" and "only one JSON object" in item["content"]
        for item in requests_for(question)[-1]["messages"]
    )


def _until_assistants(page, session_ids, timeout=10):
    """WebSocket messages for these sessions, through each session's answer."""
    seen = []
    got = set()
    deadline = time.time() + timeout

    while got != set(session_ids) and time.time() < deadline:
        pushed = json.loads(page.recv(timeout=max(0.1, deadline - time.time())))

        if pushed.get("type") in {"system", "patient"}:
            continue

        seen.append(pushed)

        if (
            pushed.get("type") == "assistant"
            and pushed.get("session_id") in session_ids
        ):
            got.add(pushed["session_id"])

    if got != set(session_ids):
        raise AssertionError(f"missing answers for {set(session_ids) - got}")

    return seen


def test_server_runs_the_agents_tool_calls(dispatch_server):
    session_id = send_chat(dispatch_server, "[read] What does the note say?")[
        "session_id"
    ]
    answer = json.loads(
        wait_for_answer(dispatch_server, session_id)["history"][-1]["content"]
    )

    assert "[cite:c1]" in answer["response"]
    citation = answer["citations"][0]
    assert citation["id"] == "c1"
    assert citation["cite"] == "c1"
    assert citation["web_path"].endswith("clinic_note.txt")
    assert citation["command"]["tool"] == "fetch"
    assert citation["command"]["arguments"]["view"] == "text"
    assert citation["command"]["arguments"]["resource_id"]


def test_a_read_arrives_as_a_step_with_its_cite_token(dispatch_server):
    question = "[read] Which file did you open?"

    with connect(dispatch_server["ws"] + "/ws") as page:
        assert json.loads(page.recv(timeout=5))["type"] == "system"
        session_id = send_chat(dispatch_server, question)["session_id"]
        seen = _until_assistants(page, [session_id])

    finished = [
        item
        for item in seen
        if item["type"] == "step"
        and item["status"] == "done"
        and item["kind"] == "tool"
    ]
    assert len(finished) == 1
    assert finished[0]["session_id"] == session_id
    assert finished[0]["name"] == "fetch"
    assert finished[0]["cite"] == "c1"
    assert finished[0]["cite_kind"] == "note"
    assert finished[0]["label"].startswith("Reading ")
    assert "fatigue" not in json.dumps(finished[0]).lower()

    answer = seen[-1]
    assert answer["type"] == "assistant"
    assert [step["kind"] for step in answer["steps"]] == ["tool"]
    assert any(step.get("cite") == "c1" for step in answer["steps"])
    saved = wait_for_answer(dispatch_server, session_id)["history"][-1]
    assert any(step.get("cite") == "c1" for step in saved["steps"])
    assert get(dispatch_server, f"/api/session/{session_id}")["steps"] == []


def test_the_page_hears_what_the_question_is_doing(dispatch_server):
    question = "[read][forget] What is the agent doing now?"

    with connect(dispatch_server["ws"] + "/ws") as page:
        assert json.loads(page.recv(timeout=5))["type"] == "system"
        session_id = send_chat(dispatch_server, question)["session_id"]
        seen = _until_assistants(page, [session_id])

    statuses = [item for item in seen if item["type"] == "status"]
    assert all(item["session_id"] == session_id for item in statuses)
    labels = [item["label"] for item in statuses]
    assert labels[0] == "Thinking"
    assert "Checking citations" in labels
    assert "Revising the answer" in labels
    assert all(isinstance(item["elapsed_ms"], int) for item in statuses)

    done = next(
        item for item in seen if item["type"] == "step" and item["status"] == "done"
    )
    assert isinstance(done["duration_ms"], int)
    assert isinstance(done["started_ms"], int)

    answer = seen[-1]
    assert isinstance(answer["duration_ms"], int)
    saved = wait_for_answer(dispatch_server, session_id)
    assert saved["history"][-1]["duration_ms"] == answer["duration_ms"]
    assert saved["status"] == "success"
    assert saved["activity"] == ""
    assert saved["elapsed_ms"] is None


def test_a_bad_tool_call_ends_as_an_error_step(dispatch_server):
    session_id = send_chat(dispatch_server, "[bad args] Try a broken fetch.")[
        "session_id"
    ]
    steps = wait_for_answer(dispatch_server, session_id)["history"][-1]["steps"]
    tools = [step for step in steps if step["kind"] == "tool"]

    assert tools
    assert all(step["status"] == "error" and "cite" not in step for step in tools)
    assert all("fatigue" not in step["label"].lower() for step in steps)


def test_steps_carry_the_session_that_asked(dispatch_server):
    with connect(dispatch_server["ws"] + "/ws") as page:
        assert json.loads(page.recv(timeout=5))["type"] == "system"
        asking = send_chat(dispatch_server, "[read] Steps stay on this session.")[
            "session_id"
        ]
        other = send_chat(dispatch_server, "Another session says hello.")["session_id"]
        seen = _until_assistants(page, [asking, other])

    tool_steps = [
        item for item in seen if item["type"] == "step" and item["kind"] == "tool"
    ]
    assert tool_steps
    assert {item["session_id"] for item in tool_steps} == {asking}
    asking_answer = wait_for_answer(dispatch_server, asking)["history"][-1]
    other_answer = wait_for_answer(dispatch_server, other)["history"][-1]
    assert any(step.get("cite") == "c1" for step in asking_answer["steps"])
    assert "steps" not in other_answer


def test_the_patient_card_sends_no_steps(dispatch_server):
    with connect(dispatch_server["ws"] + "/ws") as page:
        assert json.loads(page.recv(timeout=5))["type"] == "system"
        deadline = time.time() + 0.6

        while time.time() < deadline:
            try:
                pushed = json.loads(page.recv(timeout=0.2))
            except TimeoutError:
                break

            assert pushed["type"] in {"system", "patient"}


def test_a_plain_answer_has_no_steps(dispatch_server):
    session_id = send_chat(dispatch_server, "Just say hello please.")["session_id"]
    session = wait_for_answer(dispatch_server, session_id)
    answer = session["history"][-1]

    assert answer["content"] == "Plain answer."
    assert "steps" not in answer
    assert isinstance(answer["duration_ms"], int)
    assert session["steps"] == []


def test_server_rejects_markers_side_by_side(dispatch_server):
    question = "[read][multi] What does the note say?"
    session_id = send_chat(dispatch_server, question)["session_id"]
    answer = json.loads(
        wait_for_answer(dispatch_server, session_id)["history"][-1]["content"]
    )

    assert answer["response"] == (
        "The CT records fatigue [cite:c1]. The note records it too [cite:c1]."
    )
    assert any(
        item["role"] == "user" and "side by side" in item["content"]
        for item in requests_for(question)[-1]["messages"]
    )


def test_server_asks_the_agent_to_fix_missing_citations(dispatch_server):
    question = "[read][forget] What does the note say?"
    session_id = send_chat(dispatch_server, question)["session_id"]
    answer = json.loads(
        wait_for_answer(dispatch_server, session_id)["history"][-1]["content"]
    )

    assert "[cite:c1]" in answer["response"]
    assert any(
        item["role"] == "user" and item["content"].startswith("The server refused")
        for item in requests_for(question)[-1]["messages"]
    )


def test_server_gives_up_after_repeated_bad_citations(dispatch_server):
    session_id = send_chat(dispatch_server, "[read][never] What does the note say?")[
        "session_id"
    ]
    answer = wait_for_answer(dispatch_server, session_id)["history"][-1]

    assert "could not produce an answer" in answer["content"]


def test_server_retries_without_tools_when_the_agent_refuses_them(dispatch_server):
    question = "[no tools] Say hello."
    session_id = send_chat(dispatch_server, question)["session_id"]
    answer = wait_for_answer(dispatch_server, session_id)["history"][-1]

    assert answer["content"] == "Plain answer."
    assert ["tools" in body for body in requests_for(question)] == [True, False]


def test_the_answer_schema_is_sent_once_the_agent_has_read(dispatch_server):
    question = "[read] Which schema does the note answer use?"
    session_id = send_chat(dispatch_server, question)["session_id"]
    answer = json.loads(
        wait_for_answer(dispatch_server, session_id)["history"][-1]["content"]
    )

    assert "[cite:c1]" in answer["response"]
    bodies = requests_for(question)
    assert ["response_format" in body for body in bodies] == [False, True]
    sent = bodies[-1]["response_format"]
    assert sent["type"] == "json_schema"
    assert sent["json_schema"]["strict"] is True
    assert "Answer schema, with examples:" in bodies[0]["messages"][0]["content"]


def test_server_retries_without_the_schema_when_the_agent_refuses_it(dispatch_server):
    question = "[read][no schema] What does the note say?"
    session_id = send_chat(dispatch_server, question)["session_id"]
    answer = json.loads(
        wait_for_answer(dispatch_server, session_id)["history"][-1]["content"]
    )

    assert "[cite:c1]" in answer["response"]
    assert ["response_format" in body for body in requests_for(question)] == [
        False,
        True,
        False,
    ]
    assert all("tools" in body for body in requests_for(question))


def test_bad_tool_arguments_are_returned_to_the_agent(dispatch_server):
    question = "[bad args] What does the note say?"
    session_id = send_chat(dispatch_server, question)["session_id"]
    wait_for_answer(dispatch_server, session_id)

    tool = next(
        item
        for item in requests_for(question)[-1]["messages"]
        if item["role"] == "tool"
    )
    result = json.loads(tool["content"])
    assert result["status"] == "error"
    assert "not valid JSON" in result["message"]


def test_a_reply_that_is_not_a_chat_completion_is_reported(dispatch_server):
    session_id = send_chat(dispatch_server, "[broken] Say hello.")["session_id"]
    answer = wait_for_answer(dispatch_server, session_id)["history"][-1]

    assert "did not return a chat completion" in answer["content"]


def test_an_unreachable_agent_is_reported(server):
    session_id = send_chat(server, "Say hello.")["session_id"]
    answer = wait_for_answer(server, session_id)["history"][-1]

    assert "could not reach" in answer["content"]


def _write_tiny_ct(path: Path, series_uid: str, instance_number: int) -> None:
    import numpy as np
    from pydicom.dataset import Dataset, FileDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    file_meta = Dataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()
    ds = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.PatientID = "TEST"
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = series_uid
    ds.Modality = "CT"
    ds.SeriesDescription = "TinyTest"
    ds.InstanceNumber = instance_number
    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    ds.ImagePositionPatient = [0.0, 0.0, float(-instance_number)]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.PixelSpacing = [1.0, 1.0]
    ds.SliceThickness = 2.0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows = 8
    ds.Columns = 8
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelData = np.arange(64, dtype=np.uint16).tobytes()
    ds.save_as(path, enforce_file_format=True)


class DicomFetchAgent(BaseHTTPRequestHandler):
    """Fetches DICOM metadata, then one slice, then cites that series."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        messages = body["messages"]
        question = next(item["content"] for item in messages if item["role"] == "user")
        system = messages[0]["content"]
        resources = json.loads(system.split("Case resources: ", 1)[1].split("\n", 1)[0])
        series = next(item for item in resources if item["modality"] == "dicom")
        tool_msgs = [item for item in messages if item["role"] == "tool"]

        if question == "Fill in the patient card.":
            content = json.dumps(
                {
                    "name": "Tiny",
                    "age": "1",
                    "sex": "",
                    "blood_type": None,
                    "allergies": [],
                    "conditions": [],
                    "data_dates": {},
                }
            )
        elif len(tool_msgs) == 0:
            payload = FakeAgent.message(
                tool_calls=[
                    {
                        "id": "call_meta",
                        "type": "function",
                        "function": {
                            "name": "fetch",
                            "arguments": json.dumps(
                                {"resource_id": series["id"], "view": "metadata"}
                            ),
                        },
                    }
                ]
            )
            self._send(payload)
            return
        elif len(tool_msgs) == 1:
            payload = FakeAgent.message(
                tool_calls=[
                    {
                        "id": "call_preview",
                        "type": "function",
                        "function": {
                            "name": "fetch",
                            "arguments": json.dumps(
                                {
                                    "resource_id": series["id"],
                                    "view": "preview",
                                    "slice_index": 0,
                                    "window": "lung",
                                }
                            ),
                        },
                    }
                ]
            )
            self._send(payload)
            return
        else:
            handle = _cite_handle(tool_msgs)
            content = json.dumps(
                {
                    "response": f"The series is a chest CT [cite:{handle}].",
                    "citations": [
                        {
                            "cite": handle,
                            "summary": "Lung window, first slice.",
                        }
                    ],
                }
            )

        self._send(FakeAgent.message(content=content))

    def _send(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def test_a_dicom_citation_keeps_the_fetch(tmp_path):
    pytest.importorskip("pydicom")
    from pydicom.uid import generate_uid

    series = tmp_path / "ct"
    series.mkdir()
    _write_tiny_ct(series / "img.dcm", generate_uid(), 1)
    agent = ThreadingHTTPServer(("127.0.0.1", 0), DicomFetchAgent)
    threading.Thread(target=agent.serve_forever, daemon=True).start()

    try:
        with running_server(
            tmp_path,
            agent_url=f"http://127.0.0.1:{agent.server_address[1]}/v1",
            AGENT_MODEL="dicom",
        ) as server:
            session_id = send_chat(
                server, "What is the scan?", patient_id=tmp_path.name
            )["session_id"]
            answer = json.loads(
                wait_for_answer(server, session_id)["history"][-1]["content"]
            )
            citation = answer["citations"][0]
            preview = citation["dicom"]["previews"][-1]
            assert citation["cite"] == "c2"
            assert citation["id"] == "c2"
            assert citation["command"]["tool"] == "fetch"
            assert citation["command"]["arguments"]["view"] == "preview"
            assert citation["command"]["arguments"]["slice_index"] == 0
            assert citation["command"]["arguments"]["window"] == "lung"
            assert preview["slice_index"] == 0
            assert preview["window"] == "lung"
            assert "metadata" not in citation["dicom"]
            quoted = urllib.parse.urlencode(
                {
                    "web_path": citation["web_path"],
                    "slice_index": preview["slice_index"],
                    "window": preview["window"],
                }
            )
            with urllib.request.urlopen(
                server["http"] + "/api/dicom/preview?" + quoted
            ) as response:
                body = response.read()
                assert response.headers.get_content_type() == "image/png"
            assert body.startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        agent.shutdown()


def test_a_cite_handle_expands_to_the_call_that_made_it():
    from meddataos.core.verifier import Answer
    from meddataos.server.case import case as open_case

    calls = {
        "c1": {
            "tool": "fetch",
            "arguments": {"resource_id": "dicom:rt", "view": "metadata"},
            "file": "rt",
            "web_path": "/case/rt",
            "dicom": {"metadata": {"Modality": "RTSTRUCT", "roi_names": ["GTVp"]}},
        },
        "c2": {
            "tool": "fetch",
            "arguments": {
                "resource_id": "dicom:ct",
                "view": "preview",
                "slice_index": 77,
                "overlay": True,
            },
            "file": "ct",
            "web_path": "/case/ct",
            "dicom": {
                "previews": [
                    {
                        "web_path": "/case/ct",
                        "slice_index": 77,
                        "n_slices": 120,
                        "overlay": True,
                    }
                ],
            },
        },
        "c3": {
            "tool": "fetch",
            "arguments": {"resource_id": "note:completion", "view": "text"},
            "file": "2006-02-13-rt-completion.txt",
            "web_path": "/case/2006-02-13-rt-completion.txt",
        },
    }
    text = json.dumps(
        {
            "response": (
                "The tumour is contoured on the CT [cite:c2]. "
                "The RTSTRUCT names that contour [cite:c1]. "
                "Treatment finished in February [cite:c3]."
            ),
            "citations": [
                {"cite": "c2", "summary": "Slice 77 with the contour."},
                {"cite": "c1", "summary": "RTSTRUCT metadata."},
                {"cite": "c3", "summary": "Treatment finished."},
            ],
        }
    )

    parsed = json.loads(open_case.expand_citations(text, calls))
    preview_citation = parsed["citations"][0]
    preview = preview_citation["dicom"]["previews"][0]
    assert preview_citation["id"] == "c2"
    assert preview_citation["file"] == "ct"
    assert preview_citation["web_path"] == "/case/ct"
    assert preview_citation["command"]["arguments"]["slice_index"] == 77
    assert preview["slice_index"] == 77
    assert preview["overlay"] is True
    assert "metadata" not in preview_citation["dicom"]
    assert parsed["citations"][1]["dicom"]["metadata"]["Modality"] == "RTSTRUCT"
    assert "dicom" not in parsed["citations"][2]
    assert parsed["citations"][2]["web_path"].endswith("rt-completion.txt")
    assert Answer(text, [], [], calls).problem is None
    adjacent = text.replace(
        "The RTSTRUCT names that contour [cite:c1].",
        "[cite:c2] [cite:c1]",
    )
    assert Answer(adjacent, [], [], calls).problem


def test_every_answer_schema_example_is_valid():
    import meddataos.core.verifier.schema as answer

    for model in (
        answer.Evidence,
        answer.Conflict,
        answer.ImagingCitation,
        answer.NoteCitation,
        answer.TableCitation,
        answer.FileCitation,
        answer.DerivedCitation,
        answer.CitedAnswer,
    ):
        for example in model.model_config["json_schema_extra"]["examples"]:
            model.model_validate(example)

    assert json.loads(answer.prompt_schema())["examples"][0] == answer.EXAMPLE


def test_the_strict_answer_schema_uses_only_what_strict_mode_accepts():
    import meddataos.core.verifier.schema as answer

    schema = answer.response_format()["json_schema"]["schema"]
    rejected = {
        "oneOf",
        "const",
        "examples",
        "default",
        "title",
        "discriminator",
        "minItems",
    }

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
            return

        if not isinstance(node, dict):
            return

        assert not rejected & set(node)

        if node.get("type") == "object" and "properties" in node:
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])

        for key, value in node.items():
            walk(list(value.values()) if key in ("properties", "$defs") else value)

    walk(schema)
    assert set(schema["$defs"]) >= {
        "ImagingCitation",
        "NoteCitation",
        "TableCitation",
        "DerivedCitation",
        "DerivedResult",
    }


def test_a_dicom_call_must_be_cited_as_imaging():
    from meddataos.core.verifier import Answer

    calls = {
        "c1": {
            "tool": "fetch",
            "arguments": {
                "resource_id": "dicom:ct",
                "view": "preview",
                "slice_index": 77,
            },
            "modality": "dicom",
            "file": "ct",
            "web_path": "/case/ct",
        },
    }

    def problem(citation: dict) -> str | None:
        text = json.dumps(
            {
                "response": "The tumour sits at the glottic larynx [cite:c1].",
                "citations": [{"cite": "c1", "summary": "Slice 77.", **citation}],
            }
        )
        return Answer(text, [], [], calls).problem

    assert problem({}) is None
    assert problem({"kind": "imaging", "fields": ["SliceThickness"]}) is None
    assert problem({"kind": "imaging", "web_path": "/case/ct"}) is None

    as_note = problem({"kind": "note"})
    assert "kind 'note'" in as_note and "'imaging'" in as_note

    with_evidence = problem({"evidence": [{"claim": "Tumour", "quote": "glottic"}]})
    assert "evidence is not allowed" in with_evidence
    assert "that note's own fetch" in with_evidence


def test_a_volume_call_may_be_cited_as_derived():
    from meddataos.core.verifier import Answer

    calls = {
        "c5": {
            "tool": "fetch",
            "arguments": {
                "resource_id": "dicom:rtstruct",
                "view": "volume",
                "roi": "GTVp",
                "spacing": [2, 2, 2],
            },
            "modality": "dicom",
            "file": "rtstruct",
            "web_path": "/case/rtstruct",
        },
    }

    def problem(citation: dict) -> str | None:
        text = json.dumps(
            {
                "response": "GTVp volume is 2.592 cm³ [cite:c5].",
                "citations": [citation],
            }
        )
        return Answer(text, [], [], calls).problem

    assert (
        problem(
            {
                "kind": "derived",
                "cite": "c5",
                "method": (
                    "Voxel count on the GTVp label map from the RTSTRUCT volume fetch, "
                    "multiplied by voxel volume and converted to cm³."
                ),
                "summary": "GTVp volume is 2.592 cm³ on the resampled label map.",
                "result": {"value": 2.592, "unit": "cm³"},
                "inputs": [
                    {
                        "cite": "c5",
                        "label": "GTVp label map",
                        "detail": "roi=GTVp, spacing 2×2×2 mm",
                    }
                ],
            }
        )
        is None
    )

    # Volume is still a DICOM fetch; imaging remains valid. derived is optional.
    assert (
        problem(
            {
                "kind": "imaging",
                "cite": "c5",
                "summary": "RTSTRUCT volume mask for GTVp at 2 mm spacing.",
            }
        )
        is None
    )

    assert (
        problem(
            {
                "cite": "c5",
                "summary": "RTSTRUCT volume mask for GTVp at 2 mm spacing.",
            }
        )
        is None
    )

    missing_method = problem(
        {
            "kind": "derived",
            "cite": "c5",
            "summary": "GTVp volume is 2.592 cm³.",
            "inputs": [{"cite": "c5", "label": "GTVp label map"}],
        }
    )
    assert missing_method and "method" in missing_method

    missing_inputs = problem(
        {
            "kind": "derived",
            "cite": "c5",
            "method": "Voxel count times voxel volume.",
            "summary": "GTVp volume is 2.592 cm³.",
        }
    )
    assert missing_inputs and "inputs" in missing_inputs

    unknown_input = problem(
        {
            "kind": "derived",
            "cite": "c5",
            "method": "Voxel count times voxel volume.",
            "summary": "GTVp volume is 2.592 cm³.",
            "inputs": [{"cite": "c9", "label": "another series"}],
        }
    )
    assert unknown_input and "c9" in unknown_input

    as_note = problem(
        {
            "kind": "note",
            "cite": "c5",
            "summary": "GTVp volume is 2.592 cm³.",
        }
    )
    assert as_note and "kind 'note'" in as_note and "'imaging'" in as_note


def test_a_span_fetch_is_attached_to_the_citation():
    from meddataos.server.case import case as open_case

    span = open_case._call_span(
        {"modality": "note", "meta": {"note_date": "2005-12-15"}},
        "fetch",
        {"view": "span", "start": 0, "end": 20},
        {
            "view": "span",
            "data": {
                "text": "Consultation for radiotherapy.",
                "start": 0,
                "end": 31,
                "section": "History",
            },
        },
    )
    calls = {
        "c1": {
            "tool": "fetch",
            "arguments": {
                "resource_id": "note:consult",
                "view": "span",
                "start": 0,
                "end": 31,
            },
            "file": "consult.txt",
            "web_path": "/case/consult.txt",
            "modality": "note",
            "patient_id": "RADCURE-0006",
            "span": span,
        },
    }
    quote_text = "Consultation for radiotherapy."
    text = json.dumps(
        {
            "response": "Consultation for radiotherapy occurred on 2005-12-15 [cite:c1].",
            "citations": [
                {
                    "cite": "c1",
                    "summary": "Radiotherapy consult.",
                    "evidence": [
                        {
                            "n": 1,
                            "claim": "Consultation for radiotherapy occurred on 2005-12-15",
                            "quote": quote_text,
                            "start": 4,
                            "end": 9,
                        }
                    ],
                }
            ],
        }
    )
    from meddataos.core.verifier import Answer

    assert Answer(text, [], [], calls).problem is None
    missing_quote = json.loads(text)
    missing_quote["citations"][0]["evidence"][0].pop("quote")
    assert "quote" in (Answer(json.dumps(missing_quote), [], [], calls).problem or "")

    citation = json.loads(open_case.expand_citations(text, calls))["citations"][0]
    quote = citation["evidence"][0]
    assert citation["agent"] == "clinical_notes"
    assert quote["quote"] == "Consultation for radiotherapy."
    assert quote["start"] == 0
    assert quote["end"] == 30
    assert quote["note_date"] == "2005-12-15"
    assert quote["section"] == "History"
    assert quote["span_url"] == (
        "/api/cases/RADCURE-0006/notes/span?id=note%3Aconsult&start=0&end=30"
    )


def test_a_note_citation_must_quote_an_exact_slice():
    from meddataos.core.verifier import Answer
    from meddataos.server.case import case as open_case

    note = (
        "Patient ID: RADCURE-0006\n"
        "Date: 2005-12-15\n"
        "Note Type: Radiation Oncology Consultation\n\n"
        "Reason for Consultation:\n"
        "Assessment for radiotherapy for early glottic laryngeal cancer.\n\n"
        "History of Present Illness:\n"
        "This is an 87-year-old male referred for management of biopsy-proven disease. "
        "He reports several months of progressive hoarseness.\n\n"
        "Plan:\n"
        "Given early-stage glottic disease, recommend definitive radiation therapy alone. "
        "No concurrent chemotherapy is indicated.\n"
    )
    stored = open_case._call_span(
        {"modality": "note", "meta": {"note_date": "2005-12-15"}},
        "fetch",
        {"view": "text"},
        {"view": "text", "data": {"text": note}},
    )
    assert stored["origin"] == 0
    assert stored["text"] == note
    calls = {
        "c1": {
            "tool": "fetch",
            "arguments": {"resource_id": "note:consult", "view": "text"},
            "file": "consult.txt",
            "web_path": "/case/consult.txt",
            "modality": "note",
            "patient_id": "RADCURE-0006",
            "span": stored,
        },
    }
    quote = "Given early-stage glottic disease, recommend definitive radiation therapy alone."
    start = note.find(quote)
    cited = {
        "response": "Definitive radiation alone was recommended [cite:c1].",
        "citations": [
            {
                "cite": "c1",
                "summary": "Definitive radiation alone was recommended.",
                "evidence": [
                    {
                        "n": 1,
                        "claim": "Definitive radiation alone was recommended.",
                        "quote": quote,
                        "start": 1,
                        "end": 4,
                    }
                ],
            }
        ],
    }
    text = json.dumps(cited)
    assert Answer(text, [], [], calls).problem is None
    citation = json.loads(open_case.expand_citations(text, calls))["citations"][0]
    assert citation["evidence"][0]["quote"] == quote
    assert citation["evidence"][0]["start"] == start
    assert "Patient ID" not in citation["evidence"][0]["quote"]
    assert citation["evidence"][0]["span_url"] == (
        f"/api/cases/RADCURE-0006/notes/span?id=note%3Aconsult&start={start}&end={start + len(quote)}"
    )

    absent = json.loads(text)
    absent["citations"][0]["evidence"][0]["quote"] = (
        "chemotherapy was started yesterday"
    )
    assert "not in the text" in (
        Answer(json.dumps(absent), [], [], calls).problem or ""
    )

    missing = json.loads(text)
    missing["citations"][0].pop("evidence")
    assert "evidence" in (Answer(json.dumps(missing), [], [], calls).problem or "")

    whole = json.loads(text)
    whole["citations"][0]["evidence"] = [
        {
            "n": 1,
            "claim": "The consult.",
            "quote": note,
            "start": 0,
            "end": len(note),
        }
    ]
    assert "whole note" in (Answer(json.dumps(whole), [], [], calls).problem or "")


def test_a_note_citation_keeps_quotes_and_gains_span_links():
    from meddataos.core.verifier import Answer
    from meddataos.server.case import case as open_case

    calls = {
        "c1": {
            "tool": "fetch",
            "arguments": {
                "resource_id": "note:completion",
                "view": "span",
                "start": 120,
                "end": 152,
            },
            "file": "rt-completion.txt",
            "web_path": "/case/rt-completion.txt",
            "modality": "note",
            "patient_id": "RADCURE-0006",
        },
    }
    text = json.dumps(
        {
            "response": "Treatment finished on February 13 [cite:c1].",
            "citations": [
                {
                    "cite": "c1",
                    "summary": "The completion note records the last treatment date.",
                    "evidence": [
                        {
                            "n": 1,
                            "claim": "Treatment finished on February 13",
                            "quote": "Treatment completed February 13",
                            "start": 120,
                            "end": 152,
                        }
                    ],
                    "conflicts": [
                        {
                            "attribute": "rt.end_date",
                            "severity": "high",
                            "rationale": "Two notes give different end dates.",
                            "values": [
                                {
                                    "value": "February 13",
                                    "note_label": "rt-completion",
                                    "start": 120,
                                    "end": 152,
                                },
                                {
                                    "value": "February 20",
                                    "note_label": "follow-up",
                                    "note_id": "note:followup",
                                    "span_url": "/api/cases/RADCURE-0006/notes/span?id=note:followup&start=40&end=51",
                                    "start": 40,
                                    "end": 51,
                                },
                            ],
                        }
                    ],
                }
            ],
        }
    )

    parsed = json.loads(open_case.expand_citations(text, calls))
    citation = parsed["citations"][0]
    quote = citation["evidence"][0]
    assert citation["agent"] == "clinical_notes"
    assert citation["id"] == "c1"
    assert quote["note_id"] == "note:completion"
    assert quote["label"] == "rt-completion.txt"
    assert quote["span_url"] == (
        "/api/cases/RADCURE-0006/notes/span?id=note%3Acompletion&start=120&end=152"
    )
    assert citation["conflicts"][0]["values"][0]["span_url"].endswith(
        "start=120&end=152"
    )
    assert citation["conflicts"][0]["values"][1]["span_url"].endswith("start=40&end=51")
    assert Answer(text, [], [], calls).problem is None

    missing_quote = json.loads(text)
    missing_quote["citations"][0]["evidence"][0].pop("quote")
    assert Answer(json.dumps(missing_quote), [], [], calls).problem is None

    bad_offsets = json.loads(text)
    bad_offsets["citations"][0]["evidence"][0]["end"] = 120
    assert Answer(json.dumps(bad_offsets), [], [], calls).problem

    missing = json.dumps(
        {
            "response": "The tumour is contoured [cite:c9].",
            "citations": [{"cite": "c9", "summary": "Missing."}],
        }
    )
    assert Answer(missing, [], [], calls).problem


def test_describing_a_dicom_series_keeps_its_metadata():
    from meddataos.server.case import case as open_case

    record = open_case._call_dicom(
        {"modality": "dicom", "web_path": "/case/ct"},
        "describe",
        {},
        {
            "summary": {
                "Modality": "CT",
                "SeriesDescription": "Neck 2.0 CE",
                "roi_names": [],
            }
        },
    )

    assert record["metadata"]["Modality"] == "CT"
    assert record["metadata"]["SeriesDescription"] == "Neck 2.0 CE"
    assert "roi_names" not in record["metadata"]


def test_a_fetched_file_must_be_cited_by_its_handle():
    from meddataos.core.verifier import Answer

    calls = {
        "c1": {
            "tool": "fetch",
            "arguments": {"resource_id": "note:1", "view": "text"},
            "file": "clinic_note.txt",
            "web_path": "/case/clinic_note.txt",
        },
    }
    opened = [
        {
            "web_path": "/case/clinic_note.txt",
            "id": "note:1",
            "modality": "note",
            "label": "clinic_note.txt",
        }
    ]
    by_path = json.dumps(
        {
            "response": "The note records fatigue [cite:1].",
            "citations": [
                {
                    "id": "1",
                    "file": "clinic_note.txt",
                    "web_path": "/case/clinic_note.txt",
                    "summary": "Fatigue.",
                }
            ],
        }
    )
    by_handle = json.dumps(
        {
            "response": "The note records fatigue [cite:c1].",
            "citations": [{"cite": "c1", "summary": "Fatigue."}],
        }
    )

    assert Answer(by_path, opened, [], calls).problem
    assert Answer(by_handle, opened, [], calls).problem is None


def test_a_cited_dicom_preview_is_a_png(tmp_path):
    pytest.importorskip("pydicom")
    from pydicom.uid import generate_uid

    series = tmp_path / "ct"
    series.mkdir()
    series_uid = generate_uid()
    _write_tiny_ct(series / "img.dcm", series_uid, 1)
    (tmp_path / "note.txt").write_text("synthetic case")

    with running_server(tmp_path) as server:
        paths = get(server, "/api/dicom/previews")
        assert len(paths) == 1
        assert paths[0].startswith("/case/")
        quoted = urllib.parse.quote(paths[0], safe="")
        with urllib.request.urlopen(
            server["http"] + "/api/dicom/preview?web_path=" + quoted
        ) as response:
            body = response.read()
            assert response.headers.get_content_type() == "image/png"
        assert body.startswith(b"\x89PNG\r\n\x1a\n")


@contextmanager
def _specialist_case(session_id: str = "s1"):
    """A report with one note, one DICOM series, and one table, and one open question."""
    from meddataos.server.state import state

    previous = state.report
    state.report = {
        "resources": [
            {
                "id": "note:1",
                "modality": "note",
                "label": "completion",
                "path": "completion.txt",
            },
            {"id": "dicom:ct", "modality": "dicom", "label": "ct", "path": "Neck CT"},
            {
                "id": "structured:labs",
                "modality": "structured",
                "label": "labs",
                "path": "labs.csv",
            },
        ],
    }
    state.findings.clear()
    state.pending.add(session_id)
    try:
        yield state
    finally:
        state.report = previous
        state.findings.clear()
        state.pending.discard(session_id)
        state.calls.pop(session_id, None)


def _accept(session_id: str, specialist: str, reply: dict) -> dict:
    """Check a specialist reply the way a consult does, and keep its findings."""
    from meddataos.core.verifier import Answer
    from meddataos.server import findings
    from meddataos.server.case import case as open_case

    parsed = findings.parse_reply(json.dumps(reply))
    verdict = Answer(parsed.body, [], open_case.files(), {})
    assert verdict.problem is None, verdict.problem
    return findings.accept(session_id, specialist, parsed, parsed.body, verdict)


def test_a_finding_keeps_the_imaging_slice():
    from meddataos.server import findings

    with _specialist_case():
        told = _accept(
            "s1",
            "imaging",
            {
                "response": "The larynx is visible [cite:1].",
                "citations": [
                    {
                        "id": "1",
                        "file": "Neck CT",
                        "web_path": "/case/Neck CT",
                        "agent": "imaging",
                        "summary": "One axial slice shows the larynx.",
                        "preview": {"slice_index": 12, "window": "ct", "overlay": True},
                    }
                ],
            },
        )
        reply = "The scan shows the larynx [cite:imaging.1]."
        answer = json.loads(findings.splice("s1", reply))

    assert told["findings"][0]["id"] == "imaging.1"
    assert (
        json.loads(told["result"])["response"]
        == "The larynx is visible [cite:imaging.1]."
    )
    citation = answer["citations"][0]
    assert answer["response"] == reply
    assert (citation["id"], citation["finding"]) == ("imaging.1", "imaging.1")
    assert citation["agent"] == "imaging"
    assert citation["summary"] == "One axial slice shows the larynx."
    assert citation["dicom"] == {
        "previews": [
            {
                "web_path": "/case/Neck%20CT",
                "slice_index": 12,
                "window": "ct",
                "overlay": True,
            }
        ],
    }
    assert citation["basis"] == {
        "type": "model_read",
        "checked": "none",
        "by": "imaging",
    }


def test_the_routers_copy_of_a_finding_is_replaced_by_the_checked_one():
    from meddataos.server import findings

    checked = {
        "n": 1,
        "claim": "Treatment finished February 13",
        "quote": "Treatment completed February 13",
        "start": 120,
        "end": 151,
        "note_id": "note:1",
        "span_url": "/api/cases/P1/notes/span?id=note%3A1&start=120&end=151",
    }
    conflict = {
        "attribute": "rt.end_date",
        "severity": "high",
        "values": [{"value": "February 13"}],
    }

    with _specialist_case():
        _accept(
            "s1",
            "notes",
            {
                "response": "Treatment finished February 13 [cite:1].",
                "citations": [
                    {
                        "id": "1",
                        "file": "completion.txt",
                        "web_path": "/case/completion.txt",
                        "agent": "clinical_notes",
                        "summary": "Completion note.",
                        "evidence": [checked],
                        "conflicts": [conflict],
                    }
                ],
            },
        )
        reply = json.dumps(
            {
                "response": "Treatment finished in February [cite:notes.1].",
                "citations": [
                    {
                        "kind": "file",
                        "id": "notes.1",
                        "file": "completion.txt",
                        "web_path": "/case/completion.txt",
                        "summary": "Rewritten by the router.",
                        "evidence": [
                            {
                                "n": 2,
                                "claim": "Invented",
                                "quote": "The patient was cured in full",
                            }
                        ],
                    }
                ],
            }
        )
        answer = json.loads(findings.splice("s1", reply))

    (citation,) = answer["citations"]
    assert citation["agent"] == "clinical_notes"
    assert citation["summary"] == "Completion note."
    assert citation["conflicts"] == [conflict]
    assert citation["evidence"] == [checked]
    assert citation["basis"] == {
        "type": "quote",
        "checked": "specialist",
        "by": "notes",
    }


def test_a_router_cannot_mark_its_own_citation_as_a_finding():
    from meddataos.core.verifier import Answer
    from meddataos.server import findings
    from meddataos.server.case import case as open_case

    with _specialist_case():
        _accept(
            "s1",
            "notes",
            {
                "response": "Treatment finished February 13 [cite:1].",
                "citations": [
                    {
                        "id": "1",
                        "file": "completion.txt",
                        "web_path": "/case/completion.txt",
                        "summary": "Completion note.",
                    }
                ],
            },
        )
        forged = json.dumps(
            {
                "response": "The patient was cured [cite:x].",
                "citations": [
                    {
                        "kind": "file",
                        "id": "x",
                        "file": "labs.csv",
                        "web_path": "/case/labs.csv",
                        "summary": "Invented.",
                        "finding": "notes.1",
                        "basis": {"type": "cell", "checked": "server", "by": "notes"},
                    }
                ],
            }
        )
        spliced = findings.splice("s1", forged)
        (citation,) = json.loads(spliced)["citations"]
        verdict = Answer(spliced, [], open_case.files(), {}, findings.known("s1"))

        assert "finding" not in citation and "basis" not in citation
        # With findings to cite, a file the router did not open is not citable.
        assert "is not an opened file" in verdict.problem
        assert "notes.1" in verdict.correction()


def test_a_marker_for_a_finding_that_does_not_exist_is_refused():
    from meddataos.core.verifier import Answer
    from meddataos.server import findings
    from meddataos.server.case import case as open_case

    with _specialist_case():
        _accept(
            "s1",
            "notes",
            {
                "response": "Treatment finished February 13 [cite:1].",
                "citations": [
                    {
                        "id": "1",
                        "file": "completion.txt",
                        "web_path": "/case/completion.txt",
                        "summary": "Completion note.",
                    }
                ],
            },
        )
        reply = (
            "It finished in February [cite:notes.1]. The dose was 51 Gy [cite:notes.7]."
        )
        spliced = findings.splice("s1", reply)
        verdict = Answer(spliced, [], open_case.files(), {}, findings.known("s1"))
        plain = Answer(
            "It finished in February.", [], open_case.files(), {}, findings.known("s1")
        )

    assert verdict.problem == "[cite:notes.7] is not a finding. Findings: notes.1."
    assert plain.problem.startswith(
        "The reply cites nothing, but specialists returned findings"
    )


def test_a_reply_that_cites_no_finding_is_left_alone():
    from meddataos.server import findings

    with _specialist_case():
        reply = json.dumps({"response": "Plain [cite:1].", "citations": [{"id": "1"}]})
        assert findings.splice("s1", reply) == reply
        assert findings.splice("s1", "No markers here.") == "No markers here."


def test_a_specialist_may_abstain_or_state_a_need_without_citing():
    from meddataos.server import findings

    reply = findings.parse_reply(
        json.dumps(
            {
                "response": "Child-Pugh cannot be computed from the tables alone.",
                "citations": [],
                "needs": [
                    {
                        "what": "ascites: none, mild, or moderate to severe",
                        "why": "Child-Pugh",
                    },
                    "encephalopathy grade",
                ],
                "abstentions": ["No ammonia level in the tables."],
                "facts": [
                    {"key": "Lab.AFP", "value": 6.0, "cite": "c1"},
                    {"key": "", "value": 1, "cite": "c1"},
                ],
            }
        )
    )

    assert reply.body == "Child-Pugh cannot be computed from the tables alone."
    assert reply.needs == [
        {"what": "ascites: none, mild, or moderate to severe", "why": "Child-Pugh"},
        {"what": "encephalopathy grade"},
    ]
    assert reply.abstentions == ["No ammonia level in the tables."]
    assert reply.facts == [{"key": "lab.afp", "value": 6.0, "cite": "c1"}]

    with _specialist_case():
        from meddataos.core.verifier import Answer
        from meddataos.server.case import case as open_case

        verdict = Answer(reply.body, [], open_case.files(), {})
        told = findings.accept("s1", "tabular", reply, reply.body, verdict)

    assert told["status"] == "ok"
    assert told["needs"][0]["what"].startswith("ascites")
    assert "findings" not in told


def test_discrepancies_have_to_name_findings_the_server_holds():
    from meddataos.server import findings

    with _specialist_case():
        _accept(
            "s1",
            "notes",
            {
                "response": "The note says never smoked [cite:1].",
                "citations": [
                    {
                        "id": "1",
                        "file": "completion.txt",
                        "web_path": "/case/completion.txt",
                        "agent": "clinical_notes",
                        "summary": "Never smoked.",
                    }
                ],
            },
        )
        _accept(
            "s1",
            "tabular",
            {
                "response": "The registry row says current smoker [cite:1].",
                "citations": [
                    {
                        "id": "1",
                        "file": "labs.csv",
                        "web_path": "/case/labs.csv",
                        "summary": "Smoking Status is Current.",
                    }
                ],
            },
        )
        text = findings.splice(
            "s1",
            json.dumps(
                {
                    "response": "Smoking status is disputed [cite:notes.1].",
                    "citations": [],
                    "discrepancies": [
                        {
                            "attribute": "social_history.smoking",
                            "rationale": "The registry row and the note disagree.",
                            "values": [
                                {"value": "Never smoked", "finding": "notes.1"},
                                {"value": "Current", "finding": "tabular.1"},
                            ],
                            "preferred": "tabular.1",
                        },
                        {
                            "attribute": "tumor.size_cm",
                            "values": [
                                {"value": "3.2", "finding": "notes.1"},
                                {"value": "9.9", "finding": "imaging.4"},
                            ],
                        },
                    ],
                }
            ),
        )
        answer = json.loads(findings.keep_discrepancies("s1", text))

    (kept,) = answer["discrepancies"]
    assert kept["attribute"] == "social_history.smoking"
    assert kept["preferred"] == "tabular.1"
    assert [value["specialist"] for value in kept["values"]] == ["notes", "tabular"]
    assert kept["values"][1]["file"] == "labs.csv"
    # The table finding was not cited in the prose, so its citation is added for the page.
    assert [item["id"] for item in answer["citations"]] == ["notes.1", "tabular.1"]


def test_a_citation_that_names_no_agent_gets_the_one_for_its_file():
    from meddataos.server.case import case as open_case

    with _specialist_case():
        text = json.dumps(
            {
                "response": "Notes [cite:1]. Labs [cite:2]. Scan [cite:3]. Slice [cite:4].",
                "citations": [
                    {"id": "1", "web_path": "/case/completion.txt", "agent": None},
                    {"id": "2", "web_path": "/case/labs.csv"},
                    {"id": "3", "web_path": "/case/Neck%20CT"},
                    {
                        "id": "4",
                        "web_path": "/case/Neck%20CT",
                        "dicom": {"previews": [{}]},
                    },
                ],
            }
        )

        citations = json.loads(open_case.name_sources(text, {}))["citations"]

    assert [item.get("agent") for item in citations] == [
        "clinical_notes",
        "structured",
        "imaging",
        None,
    ]


def test_a_discrepancy_card_from_a_specialist_passes_the_answer_schema():
    import meddataos.core.verifier.schema as answer

    citation = {
        "kind": "file",
        "id": "d1",
        "file": "followup.txt",
        "web_path": "/case/followup.txt",
        "summary": "Follow-up note.",
        "evidence": [{"n": "d1", "claim": "February 20", "quote": "ended February 20"}],
    }
    assert answer.shape_problem(citation, None) is None
