"""The one rule for numbers and dates taken from a table read."""

from __future__ import annotations

from meddataos.core.datactrl.utils.provenance import (
    returned,
    unsupported,
    written_dates,
)

SERIES = {
    "data": {
        "item": "AFP",
        "unit": "ng/mL",
        "points": [
            {"row": 4, "date": "2020-01-15", "value": 8.2},
            {"row": 20, "date": "2020-07-22", "value": 42},
            {"row": 29, "date": "2020-10-19", "value": 27.3},
        ],
        "stats": {"n": 3, "change": 19.1, "pct_change": 232.9},
    },
    "asked": {"view": "series", "item": "AFP"},
}
SCORE = {
    "data": {"name": "albi", "value": -2.39, "label": "Grade 2", "at": "2020-01-15"},
    "asked": {"view": "score", "name": "albi", "at": "2020-01-15"},
}
PLATELETS = {"data": {"points": [{"row": 22, "date": "2020-07-22", "value": 118}]}}


def test_returned_values_and_rounding():
    assert returned(42, SERIES)
    assert returned("27.3", SERIES)
    # Rounded to the decimals shown, and by no more than 5 percent.
    assert returned(27, SERIES)
    assert not returned(28, SERIES)
    assert not returned(3, {"value": 2.78})


def test_row_numbers_are_positions_not_values():
    assert not returned(20, SERIES)
    assert unsupported("AFP was 29 ng/mL", SERIES) == ["29"]


def test_a_label_and_its_number_count_as_returned():
    assert returned("grade 2", SCORE)
    assert unsupported("ALBI was -2.39, grade 2, on 2020-01-15", SCORE) == []


def test_iso_dates_must_be_returned_or_asked():
    assert unsupported("AFP peaked at 42 ng/mL on 2020-07-22", SERIES) == []
    assert unsupported("AFP peaked at 42 ng/mL on 2020-07-23", SERIES) == ["2020-07-23"]


def test_written_dates_are_judged_as_dates_not_numbers():
    assert unsupported("AFP peaked at 42 ng/mL on July 22, 2020", SERIES) == []
    assert unsupported("AFP peaked at 42 ng/mL on 22 Jul 2020", SERIES) == []
    assert unsupported("AFP was 27.3 ng/mL in October 2020", SERIES) == []
    assert unsupported("AFP peaked at 42 ng/mL on July 23, 2020", SERIES) == [
        "July 23, 2020"
    ]


def test_a_bare_year_counts_when_a_returned_date_is_in_it():
    assert unsupported("AFP rose through 2020 to 42 ng/mL", SERIES) == []
    assert unsupported("AFP rose through 2021 to 42 ng/mL", SERIES) == ["2021"]


def test_a_month_word_without_a_day_or_year_is_not_a_date():
    assert written_dates("The value may change in March") == []
    assert [found for found, _, _ in written_dates("Seen on March 3")] == ["March 3"]


def test_unit_digits_signs_and_invented_numbers():
    # The 10 and 3 of 10^3/uL are part of the unit.
    assert unsupported("Platelets were 118 x 10^3/uL", PLATELETS) == []
    # A change of 19.1 may be written as a rise, and a fall without its sign.
    assert unsupported("AFP rose by 19.1 ng/mL", SERIES) == []
    assert unsupported("ALBI fell to 2.39", SCORE) == []
    assert unsupported("Bilirubin was 2.6 mg/dL", SERIES) == ["2.6"]


def test_a_formula_and_an_input_question_are_not_values():
    score = {
        "value": -2.39,
        "label": "Grade 2",
        "formula": "Grade 1 is -2.60 or lower, grade 2 is above -2.60 up to -1.39.",
        "missing": [
            {"input": "encephalopathy", "ask": "none, grade 1 to 2, or grade 3 to 4"}
        ],
    }

    assert not returned(-2.6, score)
    assert unsupported("Encephalopathy was grade 3", score) == ["3"]
