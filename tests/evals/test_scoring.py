"""The eval scorer and the question files it reads."""

from __future__ import annotations

import json
import re
from datetime import date

import pytest
from scripts.evals.run import QUESTIONS, SYNTHETIC_CASES
from scripts.evals.scoring import (
    check,
    conflict_attributes,
    load_questions,
    looks_like_abstention,
    number_matches,
    parse_answer,
    term_matches,
    verdict,
)

QUESTION_LIST = load_questions(QUESTIONS)
REAL_CASES = {"TCGA-DD-A4NF", "RADCURE-0006"}


def test_question_ids_are_unique_and_every_area_is_present():
    ids = [item["id"] for item in QUESTION_LIST]

    assert len(ids) == len(set(ids))
    assert {item["area"] for item in QUESTION_LIST} == {"notes", "tabular", "dicom"}


@pytest.mark.parametrize(
    "item", QUESTION_LIST, ids=[item["id"] for item in QUESTION_LIST]
)
def test_every_expectation_is_well_formed(item):
    for alternatives in item["expect"]:
        assert alternatives, "an empty group can never match"

        for alternative in alternatives:
            if alternative.startswith("re:"):
                re.compile(alternative[3:])
            elif alternative.startswith("date:"):
                date.fromisoformat(alternative[5:])
            else:
                # Plain alternatives are compared with lowercased response text.
                assert alternative == alternative.lower(), alternative

    if "expect_number" in item:
        assert item["expect_number"]["min"] <= item["expect_number"]["max"]

    # Something must be checked, or the question passes on any answer.
    assert item["expect"] or item.get("expect_number") or item.get("expect_abstain")


@pytest.mark.parametrize(
    "item", QUESTION_LIST, ids=[item["id"] for item in QUESTION_LIST]
)
def test_every_question_names_a_known_case(item):
    assert (
        item["patient_id"] in REAL_CASES
        or (SYNTHETIC_CASES / item["patient_id"]).is_dir()
    )


def test_a_cited_answer_and_a_plain_one_both_parse():
    cited = parse_answer(
        json.dumps({"response": "MELD 17 [cite:c1].", "citations": [{"kind": "table"}]})
    )
    fenced = parse_answer('```json\n{"response": "No A1c.", "citations": []}\n```')
    plain = parse_answer("The files do not contain an A1c.")

    assert (cited.response, len(cited.citations), cited.is_json) == (
        "MELD 17 [cite:c1].",
        1,
        True,
    )
    assert (fenced.response, fenced.citations) == ("No A1c.", [])
    assert (plain.response, plain.is_json) == (
        "The files do not contain an A1c.",
        False,
    )


@pytest.mark.parametrize(
    "written",
    [
        "2004-02-21",
        "february 21, 2004",
        "feb 21 2004",
        "21 february 2004",
        "2004/02/21",
    ],
)
def test_a_date_matches_in_its_usual_spellings(written):
    assert term_matches("date:2004-02-21", f"the ct was done on {written}.")


def test_a_date_does_not_match_a_neighbouring_day():
    assert not term_matches("date:2004-02-21", "the ct was done on 2004-02-24.")


def test_a_number_counts_only_in_range_and_next_to_its_unit():
    spec = {"min": 1.9, "max": 2.7, "units": ["ml", "cm3", "cm³"]}

    assert number_matches(spec, "gtvp is 2.25 ml [cite:c5].")
    assert number_matches(spec, "gtvp volume is 2.592 cm³.")
    assert not number_matches(spec, "gtvp is 3.4 ml.")
    # A 2 from the series number is in range but has no unit after it.
    assert not number_matches(spec, "series 2 shows the gtvp at 3.4 ml.")


def test_abstention_wording_is_recognised():
    assert looks_like_abstention("The files do not contain an HbA1c result.")
    assert not looks_like_abstention("The HbA1c was 6.2% on 2020-01-15.")


def test_conflicts_are_read_from_note_citations():
    answer = parse_answer(
        json.dumps(
            {
                "response": "Sizes disagree [cite:c1].",
                "citations": [
                    {
                        "kind": "note",
                        "conflicts": [{"attribute": "tumor.size_cm", "values": []}],
                    }
                ],
            }
        )
    )

    assert conflict_attributes(answer) == {"tumor.size_cm"}


def test_an_abstention_question_passes_on_the_wording_alone():
    item = {"expect": [], "expect_abstain": True}

    assert verdict(item, check(item, parse_answer("The files do not contain an A1c.")))
    assert not verdict(item, check(item, parse_answer("The A1c was 6.2%.")))


def test_every_asked_expectation_must_hold_and_citations_do_not_count():
    item = {"expect": [["8.2"], ["27.3"]], "expect_conflict": "tumor.size_cm"}
    uncited = parse_answer("AFP went from 8.2 to 27.3 ng/mL.")
    flagged = parse_answer(
        json.dumps(
            {
                "response": "AFP went from 8.2 to 27.3 ng/mL [cite:c1].",
                "citations": [{"conflicts": [{"attribute": "tumor.size_cm"}]}],
            }
        )
    )

    # The terms match but the expected conflict was not flagged.
    assert not verdict(item, check(item, uncited))
    assert verdict(item, check(item, flagged))
    assert verdict({"expect": [["8.2"]]}, check({"expect": [["8.2"]]}, uncited))


def test_check_reports_each_missing_group():
    item = {"expect": [["8.2"], ["27.3"]], "expect_conflict": "tumor.size_cm"}
    checks = check(item, parse_answer("AFP went from 8.2 ng/mL to 42 ng/mL."))

    assert checks.terms is False
    assert checks.missing_terms == [["27.3"]]
    assert checks.conflict is False
    assert checks.number is None


def test_tokens_add_up_over_parties_and_rows():
    from scripts.evals.run import add_tokens, answer_tokens, summarise

    answer = answer_tokens(
        {
            "router": {"calls": 3, "input": 9000, "output": 300, "cached": 4000},
            "dicom": {"calls": 2, "input": 5000, "output": 100, "cached": 0},
        }
    )
    rows = [
        {"id": "a", "area": "dicom", "passed": True, "usage": answer, "seconds": 10},
        {"id": "b", "area": "dicom", "passed": False, "usage": {}, "seconds": 20},
    ]

    assert answer == {"calls": 5, "input": 14000, "output": 400, "cached": 4000}
    assert add_tokens([answer, answer])["input"] == 28000
    assert summarise(rows)["areas"]["dicom"]["tokens"]["input"] == 14000
