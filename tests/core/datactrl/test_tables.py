"""Typed table views: column types and roles, filtered rows, series, and scores."""

from __future__ import annotations

import pytest
from meddataos.core.datactrl import Case
from meddataos.core.datactrl.errors import InvalidFetchOptions
from meddataos.core.datactrl.utils.table import (
    Table,
    find_analyte,
    parse_number,
    parse_when,
)

LABS = """\
collected_at,test,loinc,value,unit,ref_low,ref_high,flag,status,comment
2010-11-02 08:40,Albumin,1751-7,42,g/L,35,50,,final,"outside result, SI units"
2010-11-02 08:40,"Bilirubin, total",1975-2,10,umol/L,3,21,,final,"outside result, SI units"
2011-03-10 08:15,AFP,1834-1,6.0,ng/mL,0.0,6.0,,final,
2011-03-10 08:15,"Bilirubin, total",1975-2,0.5,mg/dL,0.2,1.2,,final,
2011-03-10 08:15,Albumin,1751-7,4.0,g/dL,3.5,5.0,,final,
2011-03-10 08:15,INR,6301-6,1.1,,0.9,1.1,,final,
2011-03-10 08:15,Creatinine,2160-0,0.9,mg/dL,0.7,1.3,,final,
2011-03-10 08:15,Platelets,777-3,97,10^3/uL,150,400,L,final,
2011-03-10 08:15,AST,1920-8,64,U/L,10,40,H,final,
2011-03-10 08:15,ALT,1742-6,71,U/L,7,56,H,final,
2011-03-10 08:15,HCV genotype,32286-7,1b,,,,,final,
2011-04-15 06:10,Platelets,777-3,61,10^3/uL,150,400,L,preliminary,clumps
2011-04-15 06:10,Platelets,777-3,68,10^3/uL,150,400,L,corrected,supersedes 61
2011-08-16 09:10,AFP,1834-1,2.9,ng/mL,0.0,6.0,,final,
2012-10-09 08:45,AFP,1834-1,9.6,ng/mL,0.0,6.0,H,final,
2013-11-05 08:55,AFP,1834-1,212.0,ng/mL,0.0,6.0,H,final,
2013-11-05 08:55,Albumin,1751-7,3.2,g/dL,3.5,5.0,L,final,
2013-11-05 08:55,"Bilirubin, total",1975-2,1.6,mg/dL,0.2,1.2,H,final,
2013-11-05 08:55,Sodium,2951-2,134,mg/dL,135,145,L,final,
"""

VITALS = """\
date,encounter,weight_kg,height_cm,ecog
2010-11-02,Surveillance,90.5,173,
2011-03-14,Hepatology clinic,86.0,173,
2013-11-11,Oncology clinic,76.8,173,1
"""

MEDICATIONS = """\
medication,dose,dose_unit,start_date,stop_date,status,indication
lisinopril,10,mg,2008-05-20,,active,hypertension
zolpidem,5,mg,2010-09-14,2011-04-05,discontinued,insomnia
"""

RECORD = """\
patient_id,Age,Smoking Status,Chemo,HPV,inter_norm_ratio,Stage
P1,87.3,Current,none,,[Not Available],I
"""


@pytest.fixture(scope="module")
def case(tmp_path_factory):
    root = tmp_path_factory.mktemp("tables") / "P1"
    root.mkdir()
    (root / "labs.csv").write_text(LABS)
    (root / "vitals.csv").write_text(VITALS)
    (root / "medications.csv").write_text(MEDICATIONS)
    (root / "clinical_record.csv").write_text(RECORD)
    return Case.open(root)


def table_id(case: Case, name: str) -> str:
    return next(ref.id for ref in case.report.structured if ref.label == name)


def summary(case: Case, name: str) -> dict:
    return case.describe(table_id(case, name))["summary"]


def test_cells_parse_as_numbers_and_dates():
    assert parse_number("1,234.5") == 1234.5
    assert parse_number("<5") is None
    assert parse_number("1b") is None
    assert parse_when("2011-03-10 08:15").hour == 8
    assert parse_when("03/14/2011").day == 14
    assert parse_when("6.1") is None


def test_describe_finds_the_shape_of_each_table(case):
    assert summary(case, "labs.csv")["shape"] == "long"
    assert summary(case, "vitals.csv")["shape"] == "wide"
    assert summary(case, "medications.csv")["shape"] == "intervals"
    assert summary(case, "clinical_record.csv")["shape"] == "record"


def test_describe_keeps_the_column_names_and_row_count(case):
    described = summary(case, "labs.csv")

    assert described["columns"][:3] == ["collected_at", "test", "loinc"]
    assert described["row_count"] == 19
    assert described["roles"]["item"] == "test"
    assert described["roles"]["time"] == "collected_at"
    assert described["date_range"] == ["2010-11-02", "2013-11-05"]


def test_describe_types_columns_and_lists_the_measurements(case):
    described = summary(case, "labs.csv")
    types = described["column_types"]

    assert types["collected_at"]["type"] == "date"
    # One text result among the numbers does not make the column text.
    assert types["value"]["type"] == "mixed"
    afp = next(item for item in described["items"] if item["item"] == "AFP")
    assert afp == {
        "item": "AFP",
        "n": 4,
        "first": "2011-03-10",
        "last": "2013-11-05",
        "key": "lab.afp",
        "unit": "ng/mL",
    }
    assert "child_pugh" in [score["name"] for score in described["scores"]]


def test_a_registry_answer_of_none_is_a_value_and_a_placeholder_is_not(case):
    types = summary(case, "clinical_record.csv")["column_types"]

    assert types["Chemo"]["type"] == "category"
    assert types["Chemo"]["values"] == ["none"]
    assert types["inter_norm_ratio"]["type"] == "empty"
    assert types["HPV"]["type"] == "empty"
    assert types["Age"]["type"] == "number"


def test_a_status_column_is_a_result_status_only_beside_results(case):
    assert "status" in summary(case, "labs.csv")["roles"]
    assert "status" not in summary(case, "medications.csv")["roles"]


def test_rows_filter_and_sort_and_name_the_file_rows(case):
    labs = table_id(case, "labs.csv")
    fetched = case.fetch(
        labs,
        view="rows",
        where=[
            {"column": "test", "value": "afp"},
            {"column": "value", "op": "gt", "value": 6},
            {"column": "collected_at", "op": "gt", "value": "2011-04-14"},
        ],
        order_by="collected_at",
        columns=["collected_at", "value"],
    )

    assert fetched["data"]["rows"] == [
        {"collected_at": "2012-10-09 08:45", "value": "9.6"},
        {"collected_at": "2013-11-05 08:55", "value": "212.0"},
    ]
    assert fetched["data"]["row_numbers"] == [14, 15]
    assert fetched["data"]["matched"] == 2
    assert fetched["data"]["row_count"] == 19


def test_a_date_without_a_time_covers_the_whole_day(case):
    labs = table_id(case, "labs.csv")
    same_day = case.fetch(
        labs,
        view="rows",
        limit=50,
        where=[{"column": "collected_at", "value": "2011-03-10"}],
    )

    assert same_day["data"]["matched"] == 9


def test_rows_without_a_filter_are_unchanged(case):
    labs = table_id(case, "labs.csv")
    page = case.fetch(labs, view="rows", limit=2, offset=1)

    assert [row["test"] for row in page["data"]["rows"]] == ["Bilirubin, total", "AFP"]
    assert page["data"]["row_numbers"] == [1, 2]
    assert page["bounds"] == {"limit": 2, "offset": 1, "truncated": True}
    assert "matched" not in page["data"]


def test_an_unknown_column_or_operator_is_refused(case):
    labs = table_id(case, "labs.csv")

    with pytest.raises(InvalidFetchOptions, match="Unknown column"):
        case.fetch(labs, view="rows", where=[{"column": "nope", "value": 1}])

    with pytest.raises(InvalidFetchOptions, match="Unknown op"):
        case.fetch(
            labs, view="rows", where=[{"column": "value", "op": "~", "value": 1}]
        )


def test_a_series_gives_the_points_and_their_summary(case):
    labs = table_id(case, "labs.csv")
    afp = case.fetch(labs, view="series", item="AFP", from_date="2011-04-15")["data"]

    assert afp["key"] == "lab.afp"
    assert afp["unit"] == "ng/mL"
    assert [point["value"] for point in afp["points"]] == [2.9, 9.6, 212]
    assert afp["stats"]["min"] == {"date": "2011-08-16", "value": 2.9, "row": 13}
    assert afp["stats"]["last"] == {"date": "2013-11-05", "value": 212, "row": 15}
    assert afp["stats"]["high"] == {
        "n": 2,
        "first": {"date": "2012-10-09", "value": 9.6, "row": 14},
    }
    assert afp["stats"]["change"] == 209.1
    assert afp["stats"]["days"] == 812


def test_a_value_at_the_upper_limit_is_not_flagged(case):
    labs = table_id(case, "labs.csv")
    afp = case.fetch(labs, view="series", item="lab.afp", to_date="2011-03-10")["data"]

    assert afp["points"][0]["value"] == 6
    assert afp["points"][0]["flag"] == ""


def test_a_series_brings_other_units_to_one(case):
    labs = table_id(case, "labs.csv")
    albumin = case.fetch(labs, view="series", item="1751-7")["data"]
    first = albumin["points"][0]

    assert albumin["unit"] == "g/dL"
    assert first["value"] == 4.2
    assert first["reported"] == {"value": 42, "unit": "g/L"}
    assert (first["ref_low"], first["ref_high"]) == (3.5, 5)
    bilirubin = case.fetch(labs, view="series", item="bilirubin")["data"]
    assert bilirubin["points"][0]["value"] == 0.5848


def test_a_unit_with_no_known_conversion_stays_out_of_the_summary(case):
    labs = table_id(case, "labs.csv")
    sodium = case.fetch(labs, view="series", item="Sodium")["data"]

    assert sodium["points"] == []
    assert sodium["stats"] == {"n": 0}
    assert sodium["other_units"][0]["reported"] == {"value": 134, "unit": "mg/dL"}


def test_a_corrected_result_replaces_the_preliminary_one(case):
    labs = table_id(case, "labs.csv")
    platelets = case.fetch(
        labs, view="series", item="Platelets", from_date="2011-04-15"
    )["data"]

    assert [point["value"] for point in platelets["points"]] == [68]
    assert [point["value"] for point in platelets["superseded"]] == [61]


def test_a_text_result_is_returned_beside_the_numbers(case):
    labs = table_id(case, "labs.csv")
    genotype = case.fetch(labs, view="series", item="HCV genotype")["data"]

    assert genotype["stats"] == {"n": 0}
    assert genotype["text_values"][0]["text"] == "1b"
    assert genotype["text_values"][0]["row"] == 10


def test_a_wide_table_is_read_by_column(case):
    vitals = table_id(case, "vitals.csv")
    weight = case.fetch(vitals, view="series", column="weight_kg")["data"]

    assert weight["unit"] == "kg"
    assert weight["key"] == "vital.weight"
    assert weight["stats"]["change"] == -13.7
    assert weight["stats"]["pct_change"] == -15.1
    # A visit with no ECOG recorded is not a zero.
    ecog = case.fetch(vitals, view="series", column="ecog")["data"]
    assert [point["value"] for point in ecog["points"]] == [1]


def test_a_series_names_what_the_table_has_when_the_item_is_missing(case):
    labs = table_id(case, "labs.csv")

    with pytest.raises(InvalidFetchOptions, match="No item 'troponin'.*AFP"):
        case.fetch(labs, view="series", item="troponin")

    with pytest.raises(InvalidFetchOptions, match="shape is 'intervals'"):
        case.fetch(table_id(case, "medications.csv"), view="series", column="dose")


def test_a_score_with_a_missing_input_is_incomplete_not_estimated(case):
    labs = table_id(case, "labs.csv")
    score = case.fetch(labs, view="score", name="child_pugh", at="2011-03-10")["data"]

    assert score["complete"] is False
    assert "value" not in score
    assert [item["input"] for item in score["missing"]] == ["ascites", "encephalopathy"]
    assert [item["input"] for item in score["components"]] == [
        "bilirubin",
        "albumin",
        "inr",
    ]


def test_child_pugh_uses_table_rows_and_supplied_findings(case):
    labs = table_id(case, "labs.csv")
    score = case.fetch(
        labs,
        view="score",
        name="child_pugh",
        at="2011-03-10",
        inputs={"ascites": "no ascites", "encephalopathy": "none"},
    )["data"]

    assert (score["value"], score["label"], score["complete"]) == (5, "Class A", True)
    bilirubin = score["components"][0]
    assert bilirubin["source"] == {
        "row": 3,
        "date": "2011-03-10",
        "item": "Bilirubin, total",
    }
    assert bilirubin["points"] == 1
    assert score["components"][3] == {
        "input": "ascites",
        "value": "none",
        "source": "provided",
        "points": 1,
    }


def test_a_graded_finding_must_be_one_the_score_knows(case):
    labs = table_id(case, "labs.csv")

    with pytest.raises(InvalidFetchOptions, match="ascites must be"):
        case.fetch(labs, view="score", name="child_pugh", inputs={"ascites": "unknown"})


def test_albi_at_two_dates(case):
    labs = table_id(case, "labs.csv")
    early = case.fetch(labs, view="score", name="albi", at="2011-03-10")["data"]
    late = case.fetch(labs, view="score", name="albi", at="2013-11-05")["data"]

    assert (early["value"], early["label"]) == (-2.78, "Grade 1")
    assert (late["value"], late["label"]) == (-1.77, "Grade 2")


def test_a_score_does_not_reach_outside_its_window(case):
    labs = table_id(case, "labs.csv")
    score = case.fetch(labs, view="score", name="albi", at="2012-06-01")["data"]

    assert score["complete"] is False
    assert [item["input"] for item in score["missing"]] == ["bilirubin", "albumin"]


def test_fib4_and_meld_match_their_formulas(case):
    labs = table_id(case, "labs.csv")
    fib4 = case.fetch(
        labs, view="score", name="fib4", at="2011-03-10", inputs={"age": 71}
    )["data"]
    meld = case.fetch(labs, view="score", name="meld", at="2011-03-10")["data"]

    # 71 x 64 / (97 x sqrt(71))
    assert fib4["value"] == 5.56
    # Bilirubin 0.5 and creatinine 0.9 count as 1; INR 1.1 adds 11.2 x ln(1.1).
    assert meld["value"] == 7


def test_bmi_and_interval(case):
    vitals = table_id(case, "vitals.csv")
    bmi = case.fetch(vitals, view="score", name="bmi", at="2011-03-14")["data"]
    interval = case.fetch(
        vitals,
        view="score",
        name="interval",
        inputs={"from_date": "2006-01-17", "to_date": "2007-06-28"},
    )["data"]

    assert bmi["value"] == 28.7
    assert bmi["components"][0]["source"] == {
        "row": 1,
        "column": "weight_kg",
        "date": "2011-03-14",
    }
    assert (interval["value"], interval["label"]) == (527, "1.44 years")


def test_an_unknown_score_names_the_ones_there_are(case):
    labs = table_id(case, "labs.csv")

    with pytest.raises(InvalidFetchOptions, match="available: child_pugh"):
        case.fetch(labs, view="score", name="apache")


def test_names_that_mean_two_things_are_not_keyed():
    table = Table.build(
        ["medication", "dose", "status"],
        [
            {"medication": "lisinopril", "dose": "10", "status": "active"},
        ],
    )

    assert table.roles == {}
    assert find_analyte("dose") is None
    assert find_analyte("alpha-fetoprotein").key == "lab.afp"


def test_a_test_named_with_its_unit_still_finds_the_test(case):
    labs = table_id(case, "labs.csv")

    for name in ("AFP [ng/mL]", "AFP (ng/mL)", "afp"):
        assert case.fetch(labs, view="series", item=name)["data"]["key"] == "lab.afp"


def test_an_unknown_projected_column_is_refused_not_nulled(case):
    labs = table_id(case, "labs.csv")

    with pytest.raises(
        InvalidFetchOptions,
        match="Unknown column 'date', 'reference_range'; columns: collected_at",
    ):
        case.fetch(labs, view="rows", columns=["date", "value", "reference_range"])

    # A name that differs only in case or punctuation is the table's own column.
    page = case.fetch(labs, view="rows", columns=["Collected At", "VALUE"], limit=1)
    assert page["data"]["rows"] == [{"collected_at": "2010-11-02 08:40", "value": "42"}]
    assert page["data"]["columns"] == ["collected_at", "value"]
