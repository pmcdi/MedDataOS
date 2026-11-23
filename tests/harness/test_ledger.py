"""The notes ledger as harness tools. A fixed ledger stands in for the model-built one."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from meddataos.harness.ledger import LedgerService
from meddataos.harness.notes.ledger import Ledger
from meddataos.harness.notes.schema import Fact
from meddataos.harness.tools import HarnessTools


def fact(
    fact_id: str, attribute: str, value: str, date: str, quote: str, start: int
) -> Fact:
    return Fact(
        fact_id=fact_id,
        category=attribute.split(".", 1)[0],
        attribute=attribute,
        value=value,
        unit="cm" if attribute.endswith("_cm") else None,
        normalized_value=None,
        event_date=date,
        date_kind="explicit",
        relative_time=None,
        assertion="present",
        source_type="radiology_final",
        note_id="note:rad",
        note_label="radiology",
        note_date=date,
        section=None,
        char_start=start,
        char_end=start + len(quote),
        quote=quote,
    )


LEDGER = Ledger(
    patient_id="P1",
    model="fixture",
    built_at="2026-11-18",
    facts=[
        fact("F1", "tumor.size_cm", "3.9", "2004-02-24", "mass measuring 3.9 cm", 10),
        fact(
            "F2",
            "social_history.smoking",
            "former smoker",
            "2005-12-15",
            "former smoker",
            40,
        ),
    ],
    timeline=[
        {"date": "2004-02-24", "label": "MRI liver", "date_kind": "explicit"},
        {"date": "2012-05-01", "label": "Recurrence", "date_kind": "inferred"},
    ],
    conflicts=[
        {"conflict_id": "C1", "attribute": "tumor.size_cm", "kind": "discordant"}
    ],
)
CASE = SimpleNamespace(report=SimpleNamespace(notes=["note:rad"]))


class StubReferences:
    sites: list = []


def built_service() -> LedgerService:
    service = LedgerService(build=lambda patient_id, case: LEDGER)
    service.start(CASE, "P1").join()
    return service


def test_facts_come_back_with_where_to_read_them():
    rows = built_service().query({"attribute": "tumor."})["facts"]

    assert rows == [
        {
            "fact_id": "F1",
            "attribute": "tumor.size_cm",
            "value": "3.9",
            "unit": "cm",
            "date": "2004-02-24",
            "date_kind": "explicit",
            "assertion": "present",
            "source_type": "radiology_final",
            "note_id": "note:rad",
            "note_label": "radiology",
            "quote": "mass measuring 3.9 cm",
            "start": 10,
            "end": 31,
        }
    ]


def test_the_timeline_filters_by_year_or_day():
    service = built_service()

    assert [
        event["label"] for event in service.timeline({"from_date": "2012"})["events"]
    ] == ["Recurrence"]
    assert [
        event["label"]
        for event in service.timeline({"to_date": "2004-02-24"})["events"]
    ] == ["MRI liver"]
    assert service.conflicts({})["conflicts"][0]["attribute"] == "tumor.size_cm"


def test_queries_wait_for_the_build_and_report_a_failure(tmp_path):
    release = threading.Event()

    def slow_build(patient_id, case):
        release.wait(5)
        return LEDGER

    service = LedgerService(build=slow_build)
    worker = service.start(CASE, "P1")
    tools = HarnessTools(
        skills_dir=tmp_path, references=StubReferences(), ledger=service
    )

    building = tools.run("ledger_query", {"text": "smoking"})
    release.set()
    worker.join()

    assert (
        building["status"] == "error"
        and "still being built (1 notes)" in building["message"]
    )
    assert (
        tools.run("ledger_query", {"text": "smoking"})["result"]["facts"][0]["fact_id"]
        == "F2"
    )

    def broken(patient_id, case):
        raise RuntimeError("model unreachable")

    failed = LedgerService(build=broken)
    failed.start(CASE, "P1").join()

    assert failed.status == "failed: model unreachable"


def test_the_ledger_tools_are_offered_only_for_a_case_with_notes(tmp_path):
    with_notes = HarnessTools(
        skills_dir=tmp_path, references=StubReferences(), ledger=built_service()
    )
    no_notes = LedgerService(build=lambda patient_id, case: LEDGER)
    no_notes.start(SimpleNamespace(report=SimpleNamespace(notes=[])), "P1")
    without = HarnessTools(
        skills_dir=tmp_path, references=StubReferences(), ledger=no_notes
    )

    assert {"ledger_query", "notes_timeline", "notes_conflicts"} <= with_notes.names
    assert not {"ledger_query", "notes_timeline", "notes_conflicts"} & without.names
    assert no_notes.status == "no notes"


def test_the_end_of_a_build_is_reported():
    reported = []
    service = LedgerService(
        build=lambda patient_id, case: LEDGER, on_done=reported.append
    )
    service.start(CASE, "P1").join()

    def broken(patient_id, case):
        raise RuntimeError("model unreachable")

    failing = LedgerService(build=broken, on_done=reported.append)
    failing.start(CASE, "P1").join()

    assert reported == ["ready (2 facts)", "failed: model unreachable"]
