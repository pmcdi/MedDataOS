"""The notes ledger: grounding, normalisation, conflicts, timeline, and a replay.

Moved from the notes specialist's tests; the agent and verify tests went with
the specialist. The live gold questions run through ``pixi run eval``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from meddataos.core.datactrl import Case
from meddataos.harness.notes import llm
from meddataos.harness.notes.conflicts import detect_conflicts
from meddataos.harness.notes.grounding import locate, value_supported
from meddataos.harness.notes.ingest import NoteDoc, build_ledger, facts_from_payload
from meddataos.harness.notes.schema import Fact, normalize_attribute, normalize_value
from meddataos.harness.notes.timeline import build_timeline

FIXTURES = Path(__file__).parent / "fixtures"
TCGA = "TCGA-DD-A4NF"
CASES = Path(os.getenv("MEDDATA_CASES", Path(__file__).resolve().parents[2] / "data"))
needs_tcga = pytest.mark.skipif(
    not (CASES / TCGA).is_dir(), reason=f"{TCGA} not under MEDDATA_CASES"
)


_COUNTER = iter(range(10_000))


def _fact(
    attribute,
    value,
    *,
    note="n1",
    label="note1.txt",
    source="physician",
    note_date="2020-01-01",
    category=None,
    assertion="present",
    unit=None,
    event_date=None,
    relative_time=None,
    quote=None,
    derived="native",
) -> Fact:
    attribute = normalize_attribute(attribute)
    return Fact(
        fact_id=f"F{next(_COUNTER):04d}",
        category=category or attribute.split(".")[0],
        attribute=attribute,
        value=value,
        unit=unit,
        normalized_value=normalize_value(attribute, value, unit),
        event_date=event_date,
        date_kind="explicit" if event_date else "note_date",
        relative_time=relative_time,
        assertion=assertion,
        source_type=source,
        note_id=note,
        note_label=label,
        note_date=note_date,
        section=None,
        char_start=0,
        char_end=len(quote or value),
        quote=quote or value,
        derived=derived,
    )


def _note(note_id, label, note_date, note_type="clinic"):
    return {
        "note_id": note_id,
        "label": label,
        "note_date": note_date,
        "note_date_confidence": "high" if note_date else "none",
        "note_date_flags": [],
        "note_type": note_type,
    }


def _conflicts(facts, notes=None):
    notes = notes or []
    return detect_conflicts(facts, notes, build_timeline(facts, notes))


# ── Grounding ────────────────────────────────────────────────────────────────


def test_locate_exact_normalized_fuzzy_and_missing():
    text = "Findings:\nNo  portal\n vein thrombosis.\nHepatocellular carcinoma, moderately differentiated."
    exact = locate("Findings:", text)
    assert (exact.start, exact.end, exact.method) == (0, 9, "exact")
    norm = locate("No portal vein thrombosis.", text)
    assert norm.method == "normalized" and text[norm.start : norm.end].startswith(
        "No  portal"
    )
    fuzzy = locate("Hepatocelular carcinorna, moderately differentiated", text)
    assert fuzzy.method == "fuzzy" and "carcinoma" in text[fuzzy.start : fuzzy.end]
    assert locate("HCV genotype 1a", text) is None


def test_value_supported_requires_numbers_in_quote():
    assert value_supported("3.87 cm", "mass measuring 3.87 cm")
    assert not value_supported("genotype 1a", "HCV positive")


def test_facts_from_payload_drops_ungrounded_items():
    text = "IMPRESSION: LR-5 observation segment IVA, 3.9 cm.\nNo ascites."
    doc = NoteDoc("n1", "n1", "rad.txt", "2020-01-01", "high", "radiology", text)
    items = [
        {
            "category": "tumor",
            "attribute": "tumor.size",
            "value": "3.9",
            "unit": "cm",
            "assertion": "present",
            "source_type": "radiology_final",
            "quote": "segment IVA, 3.9 cm",
        },
        {
            "category": "imaging_finding",
            "attribute": "imaging.ascites",
            "value": "ascites",
            "assertion": "negated",
            "source_type": "radiology_final",
            "quote": "No ascites.",
        },
        {
            "category": "lab",
            "attribute": "lab.hcv_genotype",
            "value": "1a",
            "assertion": "present",
            "source_type": "radiology_final",
            "quote": "HCV genotype 1a",
        },
        {
            "category": "tumor",
            "attribute": "tumor.size",
            "value": "5.1",
            "unit": "cm",
            "assertion": "present",
            "source_type": "radiology_final",
            "quote": "segment IVA, 3.9 cm",
        },
    ]
    facts, dropped = facts_from_payload(items, doc, text)
    assert [f.attribute for f in facts] == ["tumor.size_cm", "imaging.ascites"]
    for fact in facts:
        assert text[fact.char_start : fact.char_end] == fact.quote
    assert facts[0].normalized_value == 3.9
    assert sorted(d["reason"] for d in dropped) == [
        "quote_not_found",
        "value_not_in_quote",
    ]


def test_value_normalization_handles_messy_nomenclature():
    assert normalize_value("tumor.size_cm", "38.7", "mm") == 3.87
    assert normalize_value("tumor.location", "Segment IVA", None) == "segment 4a"
    assert normalize_value("tumor.location", "segment for a", None) == "segment 4a"
    assert normalize_value("tumor.margin_status", "R0 per surgeon", None) == "negative"
    assert normalize_value("tumor.grade", "moderately differentiated", None) == "G2"
    assert normalize_value("rt.dose_gy", "5100", "cGy") == 51.0
    assert normalize_attribute("tumour.location") == "tumor.location"


# ── Conflicts ────────────────────────────────────────────────────────────────


def test_segment_discordance_prefers_pathology():
    facts = [
        _fact(
            "tumor.location",
            "Segment IVA",
            source="radiology_final",
            label="mri.txt",
            note="r",
        ),
        _fact(
            "tumor.location",
            "Liver, segment 8",
            source="pathology",
            label="path.pdf",
            note="p",
        ),
    ]
    (conflict,) = _conflicts(facts)
    assert conflict["kind"] == "discordant" and conflict["severity"] == "high"
    assert conflict["preferred"]["note_label"] == "path.pdf"


def test_measurement_variance_is_not_discordance():
    facts = [
        _fact("tumor.size_cm", "3.87", unit="cm", source="radiology_final"),
        _fact("tumor.size_cm", "3.9 cm", source="radiology_final"),
        _fact("tumor.size_cm", "4", unit="centimeter", source="radiology_prelim"),
    ]
    kinds = [c["kind"] for c in _conflicts(facts)]
    assert kinds == ["measurement_variance"]


def test_wording_and_staging_systems_do_not_conflict():
    facts = [
        _fact("tumor.histology", "HCC", source="operative"),
        _fact(
            "tumor.histology",
            "Hepatocellular carcinoma NOS (8170/3)",
            source="operative",
        ),
        _fact(
            "tumor.histology",
            "moderately differentiated hepatocellular carcinoma",
            source="pathology",
        ),
        _fact("tumor.stage", "Stage I"),
        _fact("tumor.stage", "T1bN0M0"),
        _fact("tumor.stage", "LR-5", source="radiology_final"),
        _fact(
            "tumor.margin_status",
            "negative for tumor",
            assertion="negated",
            source="pathology",
        ),
        _fact("tumor.margin_status", "R0 per surgeon", source="operative"),
        _fact("tumor.location", "new hepatic lesion, local recurrence"),
        _fact("tumor.location", "segment 8", source="pathology"),
    ]
    assert _conflicts(facts) == []


def test_social_prefix_smoking_status_groups_with_social_history():
    assert (
        normalize_attribute("social.smoking_status", "social_history")
        == "social_history.smoking"
    )
    facts = [
        _fact(
            "social.smoking_status",
            "Regular smoker",
            category="social_history",
            label="2005-12-15-rad-onc-consult.txt",
            note_date="2005-12-15",
        ),
        _fact(
            "social_history.smoking",
            "Never smoked",
            label="2006-01-17-rt-start.txt",
            note_date="2006-01-17",
        ),
    ]
    (conflict,) = [
        c for c in _conflicts(facts) if c["attribute"] == "social_history.smoking"
    ]
    assert conflict["kind"] == "discordant"
    from meddataos.harness.notes.conflicts import _needs_adjudication

    assert _needs_adjudication(conflict) is False


def test_smoking_status_disagreement_is_discordant():
    facts = [
        _fact(
            "social_history.smoking_status",
            "Regular smoker",
            label="2005-12-15-rad-onc-consult.txt",
            note_date="2005-12-15",
        ),
        _fact(
            "social_history.tobacco",
            "Never smoked",
            label="2006-01-17-rt-start.txt",
            note_date="2006-01-17",
        ),
    ]
    (conflict,) = _conflicts(facts)
    assert conflict["attribute"] == "social_history.smoking"
    assert conflict["kind"] == "discordant"
    assert {v["value"] for v in conflict["values"]} == {
        "Regular smoker",
        "Never smoked",
    }


def test_negated_smoking_conflicts_despite_different_wording():
    facts = [
        _fact("social_history.smoking", "Regular smoker", assertion="present"),
        _fact("social_history.smoking", "Never smoked", assertion="negated"),
    ]
    (conflict,) = _conflicts(facts)
    assert conflict["kind"] == "assertion_conflict"
    assert conflict["attribute"] == "social_history.smoking"


def test_assertion_conflict_and_age_severity():
    facts = [
        _fact("imaging.ascites", "ascites", assertion="present", label="a.txt"),
        _fact(
            "imaging.ascites",
            "ascites",
            assertion="negated",
            label="b.txt",
            source="radiology_final",
        ),
        _fact("demographics.age", "72"),
        _fact("demographics.age", "71"),
    ]
    by_attr = {c["attribute"]: c for c in _conflicts(facts)}
    assert by_attr["imaging.ascites"]["kind"] == "assertion_conflict"
    assert by_attr["demographics.age"]["severity"] == "low"


# ── Timeline ─────────────────────────────────────────────────────────────────


def test_relative_date_anchored_to_surgery():
    surgery = _fact(
        "procedure.surgery",
        "surgical resection",
        category="procedure",
        event_date="2011",
        quote="s/p surgical resection 2011",
    )
    recurrence = _fact(
        "outcome.recurrence",
        "recurrence",
        category="outcome",
        relative_time="~22 mo post-op",
        note_date=None,
    )
    events = build_timeline([surgery, recurrence], [])
    event = next(e for e in events if e.get("fact_id") == recurrence.fact_id)
    assert event["date"] == "2012-11" and event["date_kind"] == "inferred"
    assert (
        event["anchor_fact_id"] == surgery.fact_id and "approximate" in event["flags"]
    )


def test_unanchored_relative_date_stays_relative():
    recurrence = _fact(
        "outcome.recurrence",
        "recurrence",
        category="outcome",
        relative_time="~22 mo post-op",
        note_date="2013-05-01",
    )
    (event,) = build_timeline([recurrence], [])
    assert event["date_kind"] == "relative" and event["date"] == "2013-05-01"


def test_outlier_note_date_is_flagged_as_conflict():
    notes = [
        _note("a", "a.txt", "2011-03-14"),
        _note("b", "b.txt", "2012-06-01"),
        _note("c", "c.txt", "2013-04-02"),
        _note("r", "mri.txt", "2004-02-24", "radiology"),
    ]
    conflicts = _conflicts([], notes)
    assert [(c["kind"], c["values"][0]["note_label"]) for c in conflicts] == [
        ("date_inconsistency", "mri.txt")
    ]


# ── TCGA ledger replay (real notes, fixture extraction) ─────────────────────


@needs_tcga
def test_tcga_ledger_replay_grounds_facts_and_finds_conflicts(monkeypatch):
    extraction = json.loads((FIXTURES / "tcga_extraction.json").read_text())
    monkeypatch.setenv("NOTES_ADJUDICATE", "0")

    def fake_json(prompt, **_):
        label = re.search(r"Document: (.+?) \(id ", prompt).group(1)
        return {"facts": extraction.get(label, [])}

    monkeypatch.setattr(llm, "generate_json", fake_json)
    case = Case.open(CASES / TCGA)
    ledger = build_ledger(TCGA, case, ocr=False)

    for fact in ledger.facts:
        text = case.fetch(fact.note_id, view="text")["data"]["text"]
        assert text[fact.char_start : fact.char_end] == fact.quote
    assert any(
        d.get("reason") == "quote_not_found" and "genotype" in json.dumps(d)
        for d in ledger.dropped
    )

    kinds = {(c["attribute"], c["kind"]) for c in ledger.conflicts}
    assert ("tumor.location", "discordant") in kinds
    assert ("tumor.size_cm", "discordant") in kinds
    assert ("note.date", "date_inconsistency") in kinds
    assert not any(a == "tumor.histology" and k == "discordant" for a, k in kinds)

    recurrence = [
        e
        for e in ledger.timeline
        if e.get("attribute", "").startswith(("outcome.recurrence", "recurrence"))
        and e["date_kind"] == "inferred"
    ]
    assert recurrence and all(e["date"].startswith("2012") for e in recurrence)


# ── Live gold QA (opt-in) ────────────────────────────────────────────────────

# The gold set is shared with the eval runner (meddataos.evals).
GOLD = json.loads(
    (FIXTURES.parents[2] / "evals" / "questions" / "notes.json").read_text()
)
live = pytest.mark.skipif(
    not (os.getenv("GEMINI_API_KEY") and os.getenv("NOTES_LIVE_EVAL") == "1"),
    reason="live eval: set GEMINI_API_KEY and NOTES_LIVE_EVAL=1",
)


def _matches(expected: str, summary: str) -> bool:
    """Substring match, or a regex when prefixed with 're:'."""
    if expected.startswith("re:"):
        return re.search(expected[3:], summary) is not None
    return expected in summary
