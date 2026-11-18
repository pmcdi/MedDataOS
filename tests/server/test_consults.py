"""Consults end to end: findings by id, budget, context, parallel runs, file ownership.

A scripted router and two fake specialists stand in for models. Tags in the
question choose what the router does.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_server import FakeAgent, get, running_server, send_chat, wait_for_answer

CONTEXT_HEADING = "Context from other specialists:"


def _json_post(handler: BaseHTTPRequestHandler) -> dict:
    return json.loads(handler.rfile.read(int(handler.headers.get("Content-Length", 0))))


def _send(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    data = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _resources(messages: list[dict]) -> list[dict]:
    system = messages[0]["content"]
    return json.loads(system.split("Case resources: ", 1)[1].split("\n", 1)[0])


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class Notes(BaseHTTPRequestHandler):
    """Reads the case itself and cites the note by path, as the notes specialist does."""

    requests: list[dict] = []

    def do_POST(self):
        body = _json_post(self)
        Notes.requests.append(body)
        asked = next(item["content"] for item in body["messages"] if item["role"] == "user")

        if "[slow]" in asked:
            time.sleep(0.7)

        if "[notes down]" in asked:
            _send(self, 500, {"error": {"message": "notes model down"}})
            return

        if "ascites" in asked:
            content = json.dumps({
                "response": "No ascites is recorded [cite:1].",
                "citations": [{
                    "id": "1", "agent": "clinical_notes", "file": "clinic_note.txt",
                    "web_path": "/case/clinic_note.txt", "summary": "No ascites.",
                }],
            })
        else:
            content = json.dumps({
                "response": "The note records fatigue [cite:1].",
                "citations": [{
                    "id": "1", "agent": "clinical_notes", "file": "clinic_note.txt",
                    "web_path": "/case/clinic_note.txt",
                    "summary": "Chief complaint: fatigue.",
                    "evidence": [{"n": 1, "claim": "fatigue", "quote": "fatigue",
                                  "start": 17, "end": 24}],
                }],
                "abstentions": ["Weight is not documented."],
                "facts": [{"key": "symptom.fatigue", "value": "present", "cite": "1"}],
            })

        _send(self, 200, FakeAgent.message(content=content))

    def log_message(self, *args):
        pass


class Labs(BaseHTTPRequestHandler):
    """Reads the table through the server's fetch and cites cells by token."""

    requests: list[dict] = []

    def do_POST(self):
        body = _json_post(self)
        Labs.requests.append(body)
        messages = body["messages"]
        asked = next(item["content"] for item in messages if item["role"] == "user")
        results = [item for item in messages if item["role"] == "tool"]

        # Slow once per consult: this specialist is called again with its tool result.
        if "[slow]" in asked and not results:
            time.sleep(0.7)

        if "[labs down]" in asked:
            _send(self, 500, {"error": {"message": "labs model down"}})
            return

        if "[need]" in asked and CONTEXT_HEADING not in asked:
            _send(self, 200, FakeAgent.message(content=json.dumps({
                "response": "The score cannot be computed from the tables alone.",
                "citations": [],
                "needs": [{"what": "ascites: none, mild, or moderate to severe"}],
            })))
            return

        if not results:
            table = next(r for r in _resources(messages) if r["modality"] == "structured")
            _send(self, 200, FakeAgent.message(tool_calls=[
                _tool_call("call_rows", "fetch", {"resource_id": table["id"], "view": "rows"}),
            ]))
            return

        token = json.loads(results[-1]["content"])["cite"]
        wrong = "[wrong cell]" in asked and not any(
            item["role"] == "user" and item["content"].startswith("The server refused")
            for item in messages
        )
        _send(self, 200, FakeAgent.message(content=json.dumps({
            "response": f"Hemoglobin was 13.1 [cite:{token}].",
            "citations": [{
                "kind": "table", "cite": token, "summary": "Hemoglobin 13.1.",
                "fields": ["value"],
                "cells": [{"row": 0, "column": "value", "value": "31.1" if wrong else "13.1"}],
            }],
            "facts": [{"key": "lab.hemoglobin", "value": 13.1, "cite": token}],
        })))

    def log_message(self, *args):
        pass


class Router(BaseHTTPRequestHandler):
    """Decides in code what to consult, from tags in the question."""

    requests: list[dict] = []

    def do_POST(self):
        body = _json_post(self)
        Router.requests.append(body)
        status, payload = self.respond(body)
        _send(self, status, payload)

    def respond(self, body: dict) -> tuple[int, dict]:
        messages = body["messages"]
        question = next(item["content"] for item in messages if item["role"] == "user")
        rounds = sum(1 for item in messages if item.get("tool_calls"))
        results = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
        refused = any(
            item["role"] == "user" and item["content"].startswith("The server refused")
            for item in messages
        )

        if question == "Fill in the patient card.":
            return 200, FakeAgent.message(content="{}")

        if rounds == 0:
            calls = []

            if "[own]" in question:
                note = next(r for r in _resources(messages) if r["modality"] == "note")
                calls.append(_tool_call("c0", "fetch", {"resource_id": note["id"], "view": "text"}))
            elif "[budget]" in question:
                calls += [
                    _tool_call(f"n{i}", "consult_notes", {"question": f"{question} part {i}"})
                    for i in range(4)
                ]
            else:
                if "[notes]" in question:
                    calls.append(_tool_call("n", "consult_notes", {"question": question}))

                if "[labs]" in question:
                    calls.append(_tool_call("l", "consult_labs", {"question": question}))

            if calls:
                return 200, FakeAgent.message(tool_calls=calls)

            return 200, FakeAgent.message(content="Plain answer.")

        if rounds == 1 and "[own]" in question:
            return 200, FakeAgent.message(tool_calls=[
                _tool_call("n", "consult_notes", {"question": question}),
            ])

        if rounds == 1 and "[need]" in question:
            return 200, FakeAgent.message(tool_calls=[
                _tool_call("n", "consult_notes", {"question": "Is there ascites?"}),
            ])

        if rounds == 2 and "[need]" in question:
            found = results[-1]["findings"][0]
            return 200, FakeAgent.message(tool_calls=[
                _tool_call("l2", "consult_labs", {
                    "question": question,
                    "context": f"{found['text']} ({found['id']})",
                    "resource_ids": [next(
                        r for r in _resources(messages) if r["modality"] == "structured"
                    )["id"]],
                }),
            ])

        if rounds == 1 and "[fallback]" in question:
            table = next(r for r in _resources(messages) if r["modality"] == "structured")
            return 200, FakeAgent.message(tool_calls=[
                _tool_call("r", "fetch", {"resource_id": table["id"], "view": "rows"}),
            ])

        if "[fallback]" in question:
            token = results[-1]["cite"]
            return 200, FakeAgent.message(content=json.dumps({
                "response": f"The labs specialist failed. WBC was 6.2 [cite:{token}].",
                "citations": [{
                    "kind": "table", "cite": token, "summary": "WBC 6.2.",
                    "cells": [{"row": 1, "column": "value", "value": "6.2"}],
                }],
            }))

        ids = [
            finding["id"]
            for result in results
            for finding in result.get("findings") or []
        ]

        if "[steal]" in question and not refused:
            # The token of a read the labs specialist made, cited as the router's own.
            return 200, FakeAgent.message(content=json.dumps({
                "response": "Hemoglobin was 13.1 [cite:c1].",
                "citations": [{"kind": "table", "cite": "c1", "summary": "Hemoglobin."}],
            }))

        text = " ".join(f"Finding {name} holds [cite:{name}]." for name in ids)

        if "[plain]" in question:
            return 200, FakeAgent.message(content=text)

        return 200, FakeAgent.message(content=json.dumps({"response": text, "citations": []}))

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def consult_server(tmp_path_factory):
    folder = tmp_path_factory.mktemp("consults") / "P0042"
    folder.mkdir()
    (folder / "clinic_note.txt").write_text(
        "Chief complaint: fatigue.\nPlan: follow-up in 2 weeks.\n"
    )
    (folder / "labs.csv").write_text('test,value\nhemoglobin,13.1\n"wbc, total",6.2\n')
    endpoints = [ThreadingHTTPServer(("127.0.0.1", 0), handler) for handler in (Router, Notes, Labs)]

    for endpoint in endpoints:
        threading.Thread(target=endpoint.serve_forever, daemon=True).start()

    router, notes, labs = (endpoint.server_address[1] for endpoint in endpoints)
    config = tmp_path_factory.mktemp("agents") / "agents.toml"
    config.write_text(
        "\n".join([
            "[notes]",
            f'url = "http://127.0.0.1:{notes}/v1"',
            'description = "Answers from clinical notes."',
            'modalities = ["note"]',
            "",
            "[labs]",
            f'url = "http://127.0.0.1:{labs}/v1"',
            'description = "Answers from tables."',
            'modalities = ["structured"]',
            "",
        ])
    )

    try:
        with running_server(
            folder,
            agent_url=f"http://127.0.0.1:{router}/v1",
            agents_file=str(config),
            AGENT_MODEL="router",
        ) as running:
            yield running
    finally:
        for endpoint in endpoints:
            endpoint.shutdown()


def ask(server, question: str, timeout: float = 20) -> dict:
    session_id = send_chat(server, question)["session_id"]
    return wait_for_answer(server, session_id, timeout=timeout)["history"][-1]


def bodies(handler, text: str) -> list[dict]:
    return [
        body for body in handler.requests
        if any(
            item.get("role") == "user" and text in str(item.get("content"))
            for item in body["messages"]
        )
    ]


def test_the_router_cites_findings_by_id_and_gets_the_checked_citations(consult_server):
    entry = ask(consult_server, "[notes][labs] What do the note and the labs show?")
    answer = json.loads(entry["content"])
    by_id = {item["id"]: item for item in answer["citations"]}

    assert "[cite:notes.1]" in answer["response"] and "[cite:labs.1]" in answer["response"]
    note = by_id["notes.1"]
    assert note["agent"] == "clinical_notes"
    assert note["evidence"][0]["quote"] == "fatigue"
    assert note["basis"] == {"type": "quote", "checked": "specialist", "by": "notes"}

    table = by_id["labs.1"]
    assert table["cite"] == "c1"
    assert table["web_path"] == "/case/labs.csv"
    assert table["command"]["arguments"]["view"] == "rows"
    assert table["cells"] == [{"row": 0, "column": "value", "value": "13.1"}]
    assert table["table"]["rows"] == {"0": {"test": "hemoglobin", "value": "13.1"}}
    assert table["basis"] == {"type": "cell", "checked": "server", "by": "labs"}
    assert table["agent"] == "structured"


def test_the_router_is_told_findings_facts_and_what_is_not_documented(consult_server):
    question = "[notes][labs] What is told to the router?"
    ask(consult_server, question)
    last = bodies(Router, question)[-1]
    told = {
        json.loads(item["content"])["name"]: json.loads(item["content"])
        for item in last["messages"]
        if item["role"] == "tool"
    }

    notes = told["consult_notes"]
    assert notes["findings"] == [{
        "id": "notes.1", "text": "The note records fatigue [cite:notes.1].",
        "kind": "file", "file": "clinic_note.txt", "agent": "clinical_notes",
    }]
    assert notes["abstentions"] == ["Weight is not documented."]
    assert notes["facts"] == [{"key": "symptom.fatigue", "value": "present", "finding": "notes.1"}]
    # The router's copy of the reply carries no quotes to recopy.
    assert "evidence" not in json.loads(notes["result"])["citations"][0]
    assert told["consult_labs"]["facts"][0] == {
        "key": "lab.hemoglobin", "value": 13.1, "finding": "labs.1",
    }


def test_consults_sent_in_one_turn_run_at_the_same_time(consult_server):
    entry = ask(consult_server, "[notes][labs][slow] Both specialists take a while")
    consults = [step for step in entry["steps"] if step["kind"] == "consult"]

    assert {step["name"] for step in consults} == {"notes", "labs"}
    assert all(step["duration_ms"] >= 650 for step in consults)
    assert abs(consults[0]["started_ms"] - consults[1]["started_ms"]) < 300
    # One after the other would take at least 1.4 seconds.
    assert entry["duration_ms"] < 1300


def test_a_consult_step_keeps_the_question_and_the_reply(consult_server):
    question = "[notes] What does the note say?"
    entry = ask(consult_server, question)
    (step,) = [step for step in entry["steps"] if step["kind"] == "consult"]

    assert step["question"] == question
    assert step["reply"] == "The note records fatigue [cite:notes.1]."
    assert step["findings"] == 1
    assert step["abstentions"] == ["Weight is not documented."]


def test_a_specialist_can_be_asked_again_up_to_the_budget(consult_server):
    question = "[budget] Ask notes four different things"
    entry = ask(consult_server, question)
    answer = json.loads(entry["content"])
    last = bodies(Router, question)[-1]
    told = [json.loads(item["content"]) for item in last["messages"] if item["role"] == "tool"]

    assert [item["status"] for item in told] == ["ok", "ok", "ok", "error"]
    assert "budget" in told[3]["message"]
    assert len(bodies(Notes, question)) == 3
    # Findings from separate consults keep counting up, in the order they arrive.
    assert sorted(item["id"] for item in answer["citations"]) == [
        "notes.1", "notes.2", "notes.3",
    ]


def test_a_need_is_met_by_another_specialist_and_sent_back_as_context(consult_server):
    question = "[need][labs] Compute the score"
    entry = ask(consult_server, question)
    answer = json.loads(entry["content"])
    first, second = bodies(Labs, question)[0], bodies(Labs, question)[-1]
    asked_again = next(item["content"] for item in second["messages"] if item["role"] == "user")
    router_saw = [
        json.loads(item["content"])
        for item in bodies(Router, question)[1]["messages"]
        if item["role"] == "tool"
    ]

    assert router_saw[0]["needs"] == [{"what": "ascites: none, mild, or moderate to severe"}]
    assert "findings" not in router_saw[0]
    assert CONTEXT_HEADING not in first["messages"][1]["content"]
    assert f"{CONTEXT_HEADING}\nNo ascites is recorded [cite:notes.1]. (notes.1)" in asked_again
    assert "Use these resources: structured:" in asked_again
    assert [item["id"] for item in answer["citations"]] == ["notes.1", "labs.1"]
    labs_steps = [step for step in entry["steps"] if step.get("name") == "labs"]
    assert labs_steps[0]["needs"] == ["ascites: none, mild, or moderate to severe"]
    assert labs_steps[1]["context"].startswith("No ascites is recorded")


def test_the_router_reads_an_owned_file_through_its_specialist(consult_server):
    question = "[own] Read the note yourself"
    entry = ask(consult_server, question)
    answer = json.loads(entry["content"])
    refused = json.loads(next(
        item["content"]
        for item in bodies(Router, question)[1]["messages"]
        if item["role"] == "tool"
    ))

    assert refused["status"] == "error"
    assert refused["message"].startswith("Notes are read by consult_notes.")
    assert [step["kind"] for step in entry["steps"]] == ["consult"]
    assert answer["citations"][0]["finding"] == "notes.1"


def test_the_router_may_read_the_files_of_a_specialist_that_failed(consult_server):
    entry = ask(consult_server, "[labs][labs down][fallback] The labs specialist is down")
    answer = json.loads(entry["content"])
    (citation,) = answer["citations"]

    assert citation["cells"] == [{"row": 1, "column": "value", "value": "6.2"}]
    assert citation["basis"] == {"type": "cell", "checked": "server", "by": "router"}
    assert "finding" not in citation
    assert [step["status"] for step in entry["steps"]] == ["error", "done"]


def test_a_plain_text_reply_may_cite_findings(consult_server):
    entry = ask(consult_server, "[notes][plain] Answer in plain text")
    answer = json.loads(entry["content"])

    assert answer["response"] == "Finding notes.1 holds [cite:notes.1]."
    assert answer["citations"][0]["finding"] == "notes.1"


def test_the_router_cannot_cite_a_specialists_read_as_its_own(consult_server):
    question = "[labs][steal] Cite the specialist's token"
    entry = ask(consult_server, question)
    answer = json.loads(entry["content"])
    correction = next(
        item["content"]
        for item in bodies(Router, question)[-1]["messages"]
        if item["role"] == "user" and item["content"].startswith("The server refused")
    )

    assert "citation cite 'c1' is not a tool result from this question" in correction
    assert "Findings you can cite by id, such as [cite:labs.1]" in correction
    assert answer["citations"][0]["finding"] == "labs.1"


def test_a_cell_value_the_table_does_not_hold_is_sent_back(consult_server):
    question = "[labs][wrong cell] The specialist misreads a cell"
    entry = ask(consult_server, question)
    answer = json.loads(entry["content"])
    correction = next(
        item["content"]
        for item in bodies(Labs, question)[-1]["messages"]
        if item["role"] == "user" and item["content"].startswith("The server refused")
    )

    assert "cell: row 0 value is '13.1', not '31.1'" in correction
    assert answer["citations"][0]["cells"][0]["value"] == "13.1"


def test_the_table_view_parses_the_file_and_marks_cited_rows(consult_server):
    view = get(consult_server, "/api/tables/view?web_path=/case/labs.csv&rows=1")

    assert view["columns"] == ["test", "value"]
    # A quoted comma stays inside its cell.
    assert view["rows"] == [["hemoglobin", "13.1"], ["wbc, total", "6.2"]]
    assert view["row_numbers"] == [0, 1]
    assert view["highlight"] == [1]
    assert view["column_types"] == {"test": "category", "value": "number"}


def test_the_timeline_route_is_for_the_open_case(consult_server):
    import urllib.error
    import urllib.request

    timeline = get(consult_server, "/api/patients/P0042/timeline")
    assert timeline == {"lanes": [], "range": None}

    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(consult_server["http"] + "/api/patients/other/timeline")

    assert error.value.code == 404


def test_a_registry_lists_the_modalities_a_specialist_owns(tmp_path):
    from meddataos.server.registry import load_specialists

    config = tmp_path / "agents.toml"
    config.write_text(
        '[notes]\nurl = "http://127.0.0.1:1/v1"\nmodalities = ["note"]\n\n'
        '[imaging]\nurl = "http://127.0.0.1:2/v1"\n'
    )
    found = load_specialists(str(config), ["tabular=http://127.0.0.1:3/v1"])

    assert found["notes"].modalities == ("note",)
    assert found["imaging"].modalities == ()
    assert found["tabular"].modalities == ()
    properties = found["notes"].tool()["function"]["parameters"]["properties"]
    assert set(properties) == {"question", "context", "resource_ids"}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[notes]\nurl = "http://127.0.0.1:1/v1"\nmodalities = ["text"]\n', "modalities must be a list of"),
        (
            '[a]\nurl = "http://127.0.0.1:1/v1"\nmodalities = ["note"]\n\n'
            '[b]\nurl = "http://127.0.0.1:2/v1"\nmodalities = ["note"]\n',
            "Both a and b list the modality note",
        ),
    ],
)
def test_a_bad_modality_list_stops_the_server(tmp_path, body, message):
    from meddataos.server.registry import load_specialists

    config = tmp_path / "agents.toml"
    config.write_text(body)

    with pytest.raises(SystemExit, match=message):
        load_specialists(str(config), None)
