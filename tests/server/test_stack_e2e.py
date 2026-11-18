"""The whole path with scripted models: server, orchestrator, tabular and notes specialists.

The question needs both specialists: the Child-Pugh score takes three lab values
from the table and two findings that are only in the notes. Nothing here calls a
real model. The orchestrator and the tabular specialist run their real code with
a scripted model behind each.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from meddataos.datactrl import Case
from meddataos.orchestrator import serve as orchestrator_serve
from meddataos.orchestrator.model import Model
from meddataos.specialists.tabular import serve as tabular_serve
from test_server import FakeAgent, running_server, send_chat, wait_for_answer

LABS = """\
collected_at,test,loinc,value,unit,ref_low,ref_high,flag,status
2011-03-10 08:15,AFP,1834-1,6.0,ng/mL,0.0,6.0,,final
2011-03-10 08:15,"Bilirubin, total",1975-2,0.5,mg/dL,0.2,1.2,,final
2011-03-10 08:15,Albumin,1751-7,4.0,g/dL,3.5,5.0,,final
2011-03-10 08:15,INR,6301-6,1.1,,0.9,1.1,,final
"""
NOTE = (
    "HEPATOLOGY CLINIC NOTE\nDOS: 2011-03-14\n"
    "Abd: no ascites appreciated on exam.\nNeuro: A&Ox3.\nChild-Pugh: A (per calc)\n"
)
QUESTION = "What was the Child-Pugh score at baseline, and how was it worked out?"


class OrchestratorModel(Model):
    """Plans, routes the need, and writes, by what the prompt asks for."""

    prompts: list[str] = []

    def json(self, prompt: str, system: str = ""):
        OrchestratorModel.prompts.append(prompt)

        if prompt.startswith("You route a clinician's question"):
            return {"consults": [
                {"specialist": "notes", "question": "What Child-Pugh class do the notes record?"},
                {"specialist": "tabular",
                 "question": "Compute the Child-Pugh score from the labs drawn on 2011-03-10."},
            ]}

        assert prompt.startswith("You check whether specialists have what they need")
        return {"supply": [
            {"need": 1, "specialist": "notes",
             "question": "Do the notes document ascites and hepatic encephalopathy?"},
        ]}

    def text(self, prompt: str, system: str = "") -> str:
        OrchestratorModel.prompts.append(prompt)
        return (
            "The Child-Pugh score at baseline was 5 points, class A [cite:tabular.1]. "
            "The notes record no ascites [cite:notes.2] and an alert, oriented patient "
            "[cite:notes.3]. The clinic note also gives class A [cite:notes.1]."
        )

    def forward(self, body: dict) -> dict:
        # The patient card: the only request that reaches the model as it came.
        return {"role": "assistant", "content": "{}"}


def tabular_model(prompt: str):
    if "Decide which table reads" in prompt:
        call = {"table": "labs.csv", "view": "score", "name": "child_pugh", "at": "2011-03-10"}

        if "Context from other specialists:" in prompt:
            call["inputs"] = {"ascites": "none", "encephalopathy": "none"}

        return {"calls": [call]}

    token = re.search(r"\[(c\d+)\] labs\.csv", prompt).group(1)
    return {"claims": [{
        "text": "The Child-Pugh score on 2011-03-10 was 5 points, class A",
        "read": token, "computed": True, "value": 5,
    }]}


class Notes(BaseHTTPRequestHandler):
    requests: list[str] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        asked = next(item["content"] for item in body["messages"] if item["role"] == "user")
        Notes.requests.append(asked)

        def cite(number: str, summary: str) -> dict:
            return {
                "id": number, "agent": "clinical_notes", "file": "clinic_note.txt",
                "web_path": "/case/clinic_note.txt", "summary": summary,
            }

        if "ascites" in asked:
            reply = {
                "response": "No ascites was appreciated on exam [cite:1]. "
                            "The patient was alert and oriented [cite:2].",
                "citations": [cite("1", "No ascites on exam."), cite("2", "A&Ox3.")],
            }
        else:
            reply = {
                "response": "The clinic note records Child-Pugh class A [cite:1].",
                "citations": [cite("1", "Child-Pugh: A (per calc)")],
            }

        data = json.dumps(FakeAgent.message(content=json.dumps(reply))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def stack(tmp_path_factory, monkeypatch_module):
    folder = tmp_path_factory.mktemp("stack") / "P0042"
    folder.mkdir()
    (folder / "clinic_note.txt").write_text(NOTE)
    (folder / "labs.csv").write_text(LABS)

    monkeypatch_module.setattr(tabular_serve, "generate_json", tabular_model)
    orchestrator = type("Orchestrator", (orchestrator_serve.Handler,), {"model": OrchestratorModel()})
    tabular = type("Tabular", (tabular_serve.Handler,), {
        "case": Case.open(folder), "patient_id": folder.name,
    })
    endpoints = [
        ThreadingHTTPServer(("127.0.0.1", 0), handler)
        for handler in (orchestrator, Notes, tabular)
    ]

    for endpoint in endpoints:
        threading.Thread(target=endpoint.serve_forever, daemon=True).start()

    router, notes, table = (endpoint.server_address[1] for endpoint in endpoints)
    config = tmp_path_factory.mktemp("agents") / "agents.toml"
    config.write_text("\n".join([
        "[notes]",
        f'url = "http://127.0.0.1:{notes}/v1"',
        'description = "Answers from clinical notes."',
        'modalities = ["note"]',
        "",
        "[tabular]",
        f'url = "http://127.0.0.1:{table}/v1"',
        'description = "Answers from tables, with scores."',
        'modalities = ["structured"]',
        "",
    ]))

    try:
        with running_server(
            folder,
            agent_url=f"http://127.0.0.1:{router}/v1",
            agents_file=str(config),
            AGENT_MODEL="orchestrator",
        ) as running:
            yield running
    finally:
        for endpoint in endpoints:
            endpoint.shutdown()


@pytest.fixture(scope="module")
def monkeypatch_module():
    patcher = pytest.MonkeyPatch()
    yield patcher
    patcher.undo()


@pytest.fixture(scope="module")
def answered(stack):
    session_id = send_chat(stack, QUESTION)["session_id"]
    return wait_for_answer(stack, session_id, timeout=30)["history"][-1]


def test_a_score_that_needs_two_specialists_is_computed_and_cited(answered):
    answer = json.loads(answered["content"])
    by_id = {item["id"]: item for item in answer["citations"]}

    assert answer["response"].startswith(
        "The Child-Pugh score at baseline was 5 points, class A [cite:tabular.1]."
    )
    assert set(by_id) == {"tabular.1", "notes.1", "notes.2", "notes.3"}

    score = by_id["tabular.1"]
    assert score["kind"] == "derived"
    assert score["result"] == {"value": 5, "unit": "points"}
    assert score["basis"] == {"type": "computed", "checked": "server", "by": "tabular"}
    assert score["web_path"] == "/case/labs.csv"
    # The server's own record of the call, not the specialist's account of it.
    assert score["table"]["view"] == "score"
    assert score["table"]["label"] == "Class A"
    assert [
        (item["input"], item["value"], item["points"]) for item in score["table"]["components"]
    ] == [
        ("bilirubin", 0.5, 1), ("albumin", 4, 1), ("inr", 1.1, 1),
        ("ascites", "none", 1), ("encephalopathy", "none", 1),
    ]
    assert score["table"]["components"][0]["source"] == {
        "row": 1, "date": "2011-03-10", "item": "Bilirubin, total",
    }
    assert score["table"]["components"][3]["source"] == "provided"
    assert score["inputs"][3]["detail"] == (
        "none (given with the question, not read from the table)"
    )


def test_the_trace_shows_the_need_the_supplier_and_the_second_ask(answered):
    consults = [step for step in answered["steps"] if step["kind"] == "consult"]
    reads = [step for step in answered["steps"] if step["kind"] == "tool"]

    assert [step["name"] for step in consults][2:] == ["notes", "tabular"]
    assert {step["name"] for step in consults[:2]} == {"notes", "tabular"}
    first_tabular = next(step for step in consults[:2] if step["name"] == "tabular")
    assert first_tabular["needs"] == [
        "ascites: none, mild, or moderate to severe",
        "hepatic encephalopathy: none, grade 1 to 2, or grade 3 to 4",
    ]
    assert "findings" not in first_tabular
    assert consults[2]["question"] == "Do the notes document ascites and hepatic encephalopathy?"
    # What the supplier found, then what notes had found before it was asked.
    assert consults[3]["context"] == (
        "No ascites was appreciated on exam [cite:notes.2]. (notes.2)\n"
        "The patient was alert and oriented [cite:notes.3]. (notes.3)\n"
        "The clinic note records Child-Pugh class A [cite:notes.1]. (notes.1)"
    )
    assert consults[3]["findings"] == 1
    # Both score reads show as table reads, by the specialist that made them.
    assert [(step["label"], step["cite"]) for step in reads] == [
        ("Reading labs.csv", "c1"), ("Reading labs.csv", "c2"),
    ]
    assert reads[1]["cite_kind"] == "derived"


def test_the_orchestrator_used_a_model_three_times(answered):
    kinds = [prompt.split("\n", 1)[0][:40] for prompt in OrchestratorModel.prompts]

    # Plan, route the need, write. Sending the answer back as context was code.
    assert kinds == [
        "You route a clinician's question about o",
        "You check whether specialists have what ",
        "You write the answer to a clinician's qu",
    ]
    assert "1. tabular needs: ascites: none, mild, or moderate to severe (for Child-Pugh score)" in (
        OrchestratorModel.prompts[1]
    )
    assert len(Notes.requests) == 2
