"""Facts from several specialists in one store, and what the sources disagree on."""

from __future__ import annotations

from meddataos.specialists.facts import FactStore, canonical_key


def note(key, value, finding, date, assertion="present", label="note"):
    return {
        "key": key, "value": value, "finding": finding, "date": date,
        "assertion": assertion, "source_type": "physician", "label": label,
    }


def cell(key, value, finding, date=None, source_type="registry", unit=None):
    return {
        "key": key, "value": value, "finding": finding, "date": date,
        "source_type": source_type, "label": "table.csv", "unit": unit,
    }


def test_keys_are_brought_to_one_spelling():
    assert canonical_key("Lab.T_Bili") == "lab.bilirubin_total"
    assert canonical_key("lab.plt") == "lab.platelets"
    assert canonical_key("social.smoking_status") == "social_history.smoking"
    assert canonical_key("lab.afp") == "lab.afp"


def test_a_table_breaks_a_tie_between_two_notes():
    store = FactStore()
    store.add("notes", [
        note("social_history.smoking", "Currently smokes daily", "notes.1", "2005-12-15", label="consult"),
        note("social_history.smoking", "Never smoked", "notes.2", "2006-01-17", "negated", label="rt-start"),
    ])
    store.add("tabular", [cell("social_history.smoking", "Current", "tabular.1")])

    (found,) = store.discrepancies()

    assert found["attribute"] == "social_history.smoking"
    assert found["kind"] == "cross_source_assertion"
    assert [(v["finding"], v["value"], v["side"]) for v in found["values"]] == [
        ("notes.1", "Currently smokes daily", 0),
        ("tabular.1", "Current", 0),
        ("notes.2", "Never smoked", 1),
    ]
    # The later note would win on recency alone. Two sources agreeing outweighs it.
    assert found["preferred"] == "notes.1"
    assert "is recorded by notes and tabular" in found["rationale"]


def test_an_age_one_year_apart_is_low_severity_and_the_registry_sides_with_one_note():
    store = FactStore()
    store.add("notes", [
        note("demographics.age", "72", "notes.1", "2011-03-14"),
        note("age", "71", "notes.1", "2011-03-14"),
    ])
    store.add("tabular", [cell("demographics.age", "71", "tabular.2")])

    (found,) = store.discrepancies()

    assert (found["attribute"], found["kind"], found["severity"]) == (
        "demographics.age", "cross_source", "low",
    )
    sides = {v["value"]: v["side"] for v in found["values"]}
    assert sides["72"] != sides["71"]
    assert found["preferred"] in ("notes.1", "tabular.2")
    assert "'71' is recorded by notes and tabular" in found["rationale"]


def test_a_lab_value_is_compared_only_on_the_same_date():
    store = FactStore()
    store.add("notes", [note("lab.afp", "6.0", "notes.1", "2011-03-10")])
    store.add("tabular", [
        cell("lab.afp", 6.0, "tabular.1", "2011-03-10", "lab_table", "ng/mL"),
        cell("lab.afp", 212.0, "tabular.1", "2013-11-05", "lab_table", "ng/mL"),
    ])

    assert store.discrepancies() == []

    store.add("tabular", [cell("lab.platelets", 79, "tabular.2", "2011-03-10", "lab_table")])
    store.add("notes", [note("lab.plt", "97", "notes.2", "2011-03-10")])
    (found,) = store.discrepancies()

    assert found["attribute"] == "lab.platelets"
    # The lab system's own result outranks a value transcribed into a note.
    assert found["preferred"] == "tabular.2"
    assert "lab table" in found["rationale"]


def test_disagreement_inside_one_source_is_left_to_that_source():
    store = FactStore()
    store.add("notes", [
        note("tumor.location", "segment 4a", "notes.1", "2011-03-18"),
        note("tumor.location", "segment 8", "notes.2", "2011-04-14"),
    ])

    assert store.discrepancies() == []


def test_a_negated_finding_reads_as_one():
    store = FactStore()
    store.add("notes", [note("imaging.ascites", "ascites", "notes.1", "2011-03-14", "negated")])
    store.add("imaging", [cell("imaging.ascites", "ascites", "imaging.1", "2011-03-18", "imaging_measurement")])

    (found,) = store.discrepancies()

    assert [v["value"] for v in found["values"]] == ["ascites", "no ascites"]


def test_unusable_facts_are_left_out():
    store = FactStore()
    kept = store.add("tabular", [
        {"key": "lab.afp", "value": None, "finding": "tabular.1"},
        {"key": "lab.afp", "value": 6.0},
        {"value": 6.0, "finding": "tabular.1"},
        cell("lab.afp", 6.0, "tabular.1", "2011-03-10"),
        cell("lab.afp", 6.0, "tabular.1", "2011-03-10"),
    ])

    assert kept == 1 and len(store) == 1


def test_an_age_in_whole_years_matches_the_same_age_with_decimals():
    store = FactStore()
    store.add("notes", [note("demographics.age", "87-year-old", "notes.1", "2005-12-15")])
    store.add("tabular", [cell("demographics.age", "87.3", "tabular.1")])

    assert store.discrepancies() == []

    store.add("tabular", [cell("demographics.age", "88.1", "tabular.2")])

    assert [item["attribute"] for item in store.discrepancies()] == ["demographics.age"]
