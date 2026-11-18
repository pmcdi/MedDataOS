"""What the notes specialist sends back beside its citations."""

from __future__ import annotations

import json
from types import SimpleNamespace

from meddataos.datactrl import Case
from meddataos.specialists.common import latest_request, parse_request
from meddataos.specialists.notes import agent as notes_agent
from meddataos.specialists.notes import serve as notes_serve
from meddataos.specialists.notes.ledger import Ledger
from meddataos.specialists.notes.schema import Fact
from meddataos.specialists.notes.verify import Claim, Evidence, VerifiedAnswer


def test_a_consult_message_splits_into_question_context_and_resources():
    text = (
        "What was the score?\n\n"
        "Context from other specialists:\nNo ascites (notes.1)\n\nA&Ox3 (notes.2)\n\n"
        "Use these resources: structured:a, structured:b"
    )

    request = parse_request(text)

    assert request.question == "What was the score?"
    assert request.context == "No ascites (notes.1)\n\nA&Ox3 (notes.2)"
    assert request.resource_ids == ["structured:a", "structured:b"]
    assert parse_request("Just a question").context == ""


def test_the_latest_request_skips_a_correction_turn():
    messages = [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "an answer"},
        {"role": "user", "content": "Second question\n\nUse these resources: dicom:1"},
        {"role": "assistant", "content": "refused"},
        {"role": "user", "content": "The server refused that answer. Fix the markers."},
    ]

    request = latest_request(messages)

    assert request.question == "Second question"
    assert request.resource_ids == ["dicom:1"]


def _case(tmp_path) -> Case:
    (tmp_path / "consult.txt").write_text("Date: 2005-12-15\nRegular smoker. 25 pack-year history.\n")
    return Case.open(tmp_path)


def test_the_notes_reply_carries_abstentions_and_keyed_facts(tmp_path):
    case = _case(tmp_path)
    note = case.report.notes[0]
    result = SimpleNamespace(
        summary="The patient is a regular smoker [1]. Not documented in the notes: alcohol use.",
        evidence=[{"n": 1, "note_id": note.id, "quote": "Regular smoker.", "claim": "smoker"}],
        conflicts=[],
        abstentions=["alcohol use"],
        facts=[
            {"n": 1, "key": "social_history.smoking", "value": "Regular smoker", "unit": None,
             "date": "2005-12-15", "source_type": "physician", "assertion": "present",
             "label": "consult.txt"},
            {"n": 2, "key": "lab.afp", "value": "6.0", "date": None, "source_type": "physician",
             "assertion": "present", "label": "consult.txt", "unit": "ng/mL"},
        ],
    )

    reply = json.loads(notes_serve.cited_reply(case, result))

    assert reply["response"].startswith("The patient is a regular smoker [cite:1].")
    assert reply["abstentions"] == ["alcohol use"]
    # Only the fact of a claim that became a citation is sent, tied to its marker.
    assert reply["facts"] == [{
        "key": "social_history.smoking", "value": "Regular smoker", "date": "2005-12-15",
        "source_type": "physician", "assertion": "present", "label": "consult.txt", "cite": "1",
    }]


def test_a_notes_reply_with_nothing_to_cite_still_says_what_is_missing(tmp_path):
    case = _case(tmp_path)
    nothing = SimpleNamespace(
        summary="Not documented in the notes: the HCV genotype.",
        evidence=[], conflicts=[], abstentions=["the HCV genotype"], facts=[],
    )
    plain = SimpleNamespace(summary="The notes did not yield evidence.", evidence=[], conflicts=[], abstentions=[], facts=[])

    assert json.loads(notes_serve.cited_reply(case, nothing)) == {
        "response": "Not documented in the notes: the HCV genotype.",
        "citations": [],
        "abstentions": ["the HCV genotype"],
    }
    assert notes_serve.cited_reply(case, plain) == "The notes did not yield evidence."


def test_ledger_facts_behind_a_claim_are_found_by_id_or_by_span():
    def fact(fact_id, attribute, value, start, end):
        return Fact(
            fact_id=fact_id, category=attribute.split(".")[0], attribute=attribute, value=value,
            unit=None, normalized_value=value, event_date=None, date_kind="note_date",
            relative_time=None, assertion="present", source_type="physician", note_id="note:1",
            note_label="consult.txt", note_date="2005-12-15", section=None,
            char_start=start, char_end=end, quote="",
        )

    ledger = Ledger("P1", "", "", facts=[
        fact("F1", "social_history.smoking", "Regular smoker", 17, 32),
        fact("F2", "social_history.pack_years", "25", 33, 53),
        fact("F3", "performance.ecog", "2", 200, 210),
    ])

    def evidence(start, end, fact_id=None):
        return Evidence("note:1", "consult.txt", "2005-12-15", start, end, "", "native", fact_id, "quote")

    verified = VerifiedAnswer(claims=[
        Claim("The patient smokes", [evidence(17, 32, "F1")]),
        Claim("25 pack-years", [evidence(30, 60)]),
    ])

    rows = notes_agent._fact_rows(verified, ledger, None)

    assert [(row["n"], row["key"], row["value"]) for row in rows] == [
        (1, "social_history.smoking", "Regular smoker"),
        # No fact id on the second claim: the facts inside its quoted span are used.
        (2, "social_history.smoking", "Regular smoker"),
        (2, "social_history.pack_years", "25"),
    ]
    assert rows[0]["date"] == "2005-12-15" and rows[0]["source_type"] == "physician"


def test_context_frames_the_notes_question_without_becoming_evidence(tmp_path, monkeypatch):
    case = _case(tmp_path)
    ledger = notes_agent._empty_ledger("P1", case)
    monkeypatch.setenv("NOTES_ENTAILMENT", "0")
    prompts = []

    def fake_json(prompt, **_):
        prompts.append(prompt)
        return {"claims": []}

    monkeypatch.setattr(notes_agent.llm, "generate_json", fake_json)
    notes_agent.answer_question(
        "P1", "Does the patient smoke?", case=case, ledger=ledger,
        context="The registry row says Current (tabular.1)",
    )

    assert "Question: Does the patient smoke?\n\nContext from other specialists" in prompts[0]
    assert "The registry row says Current (tabular.1)" in prompts[0]
    assert "do not cite it" in prompts[0]


def test_a_notes_conflict_goes_only_on_the_citations_it_involves(tmp_path):
    (tmp_path / "consult.txt").write_text(
        "Date: 2005-12-15\nThis is an 87-year-old male.\nCurrently smokes daily.\n"
    )
    (tmp_path / "rt-start.txt").write_text("Date: 2006-01-17\nNever smoked.\n")
    case = Case.open(tmp_path)
    consult = next(ref.id for ref in case.report.notes if ref.label == "consult.txt")
    start = next(ref.id for ref in case.report.notes if ref.label == "rt-start.txt")
    smoking = {
        "conflict_id": "C1", "attribute": "social_history.smoking", "kind": "assertion_conflict",
        "severity": "medium", "rationale": "Present in one note, negated in the other.",
        "values": [
            {"value": "Currently smokes daily", "note_id": consult, "note_label": "consult.txt",
             "fact_id": "Fsmoke", "char_start": 46, "char_end": 68},
            {"value": "Never smoked", "note_id": start, "note_label": "rt-start.txt",
             "fact_id": "Fnever", "char_start": 17, "char_end": 30},
        ],
    }
    result = SimpleNamespace(
        summary=(
            "The patient was 87 years old [1]. The consultation records daily smoking [2]. "
            "The treatment start note says never smoked [3]."
        ),
        evidence=[
            {"n": 1, "note_id": consult, "quote": "87-year-old male", "start": 28, "end": 44, "fact_id": "Fage"},
            # Cited by fact id.
            {"n": 2, "note_id": consult, "quote": "Currently smokes daily", "start": 46, "end": 68, "fact_id": "Fsmoke"},
            # No fact id: found by the quote's place in the note.
            {"n": 3, "note_id": start, "quote": "Never smoked", "start": 17, "end": 29},
        ],
        conflicts=[smoking], abstentions=[], facts=[],
    )

    citations = {item["id"]: item for item in json.loads(notes_serve.cited_reply(case, result))["citations"]}

    assert citations["1"]["conflicts"] == []
    assert [item["attribute"] for item in citations["2"]["conflicts"]] == ["social_history.smoking"]
    assert [item["attribute"] for item in citations["3"]["conflicts"]] == ["social_history.smoking"]
