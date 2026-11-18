"""What the server checks on a citation of a table: cells, and computed values."""

from __future__ import annotations

import json

import pytest

from meddataos.datactrl import Case
from meddataos.server.case import Answer, Contract, case as server_case

LABS = """\
collected_at,test,loinc,value,unit,ref_low,ref_high,flag,status
2011-03-10 08:15,AFP,1834-1,6.0,ng/mL,0.0,6.0,,final
2011-03-10 08:15,"Bilirubin, total",1975-2,0.5,mg/dL,0.2,1.2,,final
2011-03-10 08:15,Albumin,1751-7,4.0,g/dL,3.5,5.0,,final
2011-04-15 06:10,Platelets,777-3,61,10^3/uL,150,400,L,preliminary
2011-04-15 06:10,Platelets,777-3,68,10^3/uL,150,400,L,corrected
2011-08-16 09:10,AFP,1834-1,2.9,ng/mL,0.0,6.0,,final
2012-10-09 08:45,AFP,1834-1,9.6,ng/mL,0.0,6.0,H,final
"""


@pytest.fixture(scope="module")
def calls(tmp_path_factory) -> dict[str, dict]:
    """Three table fetches as the server remembers them: rows, a series, a score."""
    root = tmp_path_factory.mktemp("cited") / "P1"
    root.mkdir()
    (root / "labs.csv").write_text(LABS)
    opened = Case.open(root)
    table = opened.report.structured[0]
    ref = {"modality": "structured"}

    def call(**arguments) -> dict:
        result = opened.fetch(table.id, **arguments)
        return {
            "tool": "fetch",
            "arguments": {"resource_id": table.id, **arguments},
            "file": "labs.csv",
            "web_path": "/case/labs.csv",
            "modality": "structured",
            "by": "router",
            "table": server_case._call_table(ref, "fetch", result),
        }

    return {
        "c1": call(view="rows", limit=3),
        "c2": call(view="series", item="AFP"),
        "c3": call(view="score", name="albi", at="2011-03-10"),
        "c4": call(view="series", item="Platelets"),
    }


def judge(calls: dict, response: str, citation: dict) -> str | None:
    text = json.dumps({"response": response, "citations": [citation]})
    return Answer(text, [], [], calls).problem


def test_a_cell_is_checked_against_the_fetched_row(calls):
    good = {
        "kind": "table", "cite": "c1", "summary": "AFP 6.0.",
        "cells": [{"row": 0, "column": "VALUE", "value": 6}],
    }
    wrong = {**good, "cells": [{"row": 0, "column": "value", "value": "6.5"}]}
    unseen = {**good, "cells": [{"row": 5, "column": "value", "value": "2.9"}]}
    no_column = {**good, "cells": [{"row": 0, "column": "result"}]}

    assert judge(calls, "AFP was 6.0 [cite:c1].", good) is None
    assert "cell: row 0 value is '6.0', not '6.5'" in judge(calls, "AFP was 6.5 [cite:c1].", wrong)
    assert "row 5 is not a row that fetch returned" in judge(calls, "AFP was 2.9 [cite:c1].", unseen)
    assert "row 0 has no column 'result'" in judge(calls, "AFP was 6.0 [cite:c1].", no_column)


def test_a_table_citation_without_cells_still_passes(calls):
    citation = {"kind": "table", "cite": "c1", "summary": "Lab table.", "fields": ["value"]}

    assert judge(calls, "The table lists AFP [cite:c1].", citation) is None


def test_a_series_point_is_cited_by_its_file_row(calls):
    citation = {
        "kind": "table", "cite": "c2", "summary": "AFP 9.6 on 2012-10-09.",
        "cells": [{"row": 6, "value": 9.6}],
    }
    wrong = {**citation, "cells": [{"row": 6, "value": 96}]}

    assert judge(calls, "AFP rose to 9.6 [cite:c2].", citation) is None
    assert "row 6 holds 9.6, not 96" in judge(calls, "AFP rose to 96 [cite:c2].", wrong)


def test_a_superseded_result_can_be_cited_as_what_it_was(calls):
    citation = {
        "kind": "table", "cite": "c4", "summary": "Preliminary 61, corrected to 68.",
        "cells": [{"row": 3, "value": 61}, {"row": 4, "value": 68}],
    }

    assert judge(calls, "The preliminary count of 61 was corrected to 68 [cite:c4].", citation) is None


def test_a_derived_value_has_to_be_one_the_call_returned(calls):
    def derived(value, unit="ng/mL"):
        return {
            "kind": "derived", "cite": "c2",
            "method": "Lowest value in the AFP series.",
            "summary": "AFP nadir.",
            "result": {"value": value, "unit": unit},
            "inputs": [{"cite": "c2", "label": "AFP series"}],
        }

    assert judge(calls, "The AFP nadir was 2.9 [cite:c2].", derived(2.9)) is None
    # The change from first to last is in the series summary.
    assert judge(calls, "AFP rose by 3.6 [cite:c2].", derived(3.6)) is None
    problem = judge(calls, "The mean AFP was 6.17 [cite:c2].", derived(6.17))
    assert "result 6.17 is not a value its call returned" in problem
    assert "not from arithmetic on cells" in problem


def test_a_derived_value_may_be_rounded_and_may_be_a_label(calls):
    def albi(value):
        return {
            "kind": "derived", "cite": "c3", "method": "ALBI from the score fetch.",
            "summary": "ALBI.", "result": {"value": value, "unit": None},
            "inputs": [{"cite": "c3", "label": "bilirubin and albumin"}],
        }

    assert judge(calls, "ALBI was -2.78 [cite:c3].", albi(-2.78)) is None
    assert judge(calls, "ALBI was -2.8 [cite:c3].", albi(-2.8)) is None
    assert judge(calls, "ALBI was grade 1 [cite:c3].", albi("Grade 1")) is None
    assert "is not a value its call returned" in judge(calls, "ALBI was -2.6 [cite:c3].", albi(-2.6))
    assert "is not a value its call returned" in judge(calls, "ALBI was grade 2 [cite:c3].", albi("Grade 2"))


def test_a_derived_value_from_a_table_needs_its_result(calls):
    citation = {
        "kind": "derived", "cite": "c2", "method": "From the series.",
        "summary": "AFP rose.", "inputs": [{"cite": "c2", "label": "AFP series"}],
    }

    assert "needs result.value" in judge(calls, "AFP rose [cite:c2].", citation)


def test_a_row_number_is_not_a_returned_value(calls):
    # Row 5 holds the nadir. 5 itself is not a value of the series.
    assert Contract.returned(2.9, [calls["c2"]["table"]])
    assert not Contract.returned(5, [calls["c2"]["table"]])


def test_a_delivered_table_citation_carries_checked_cells_and_the_series(calls):
    text = json.dumps({
        "response": "AFP rose to 9.6 [cite:c2]. Baseline AFP was 6.0 [cite:c1].",
        "citations": [
            {"kind": "table", "cite": "c2", "summary": "AFP.", "cells": [{"row": 6}]},
            {"kind": "table", "cite": "c1", "summary": "AFP.",
             "cells": [{"row": 0, "column": "value"}]},
        ],
    })
    delivered = json.loads(server_case.stamp_basis(server_case.expand_citations(text, calls), calls))
    series, rows = delivered["citations"]

    assert series["cells"] == [{
        "row": 6, "column": "value", "value": 9.6, "date": "2012-10-09", "unit": "ng/mL",
    }]
    assert [point["value"] for point in series["table"]["points"]] == [6, 2.9, 9.6]
    assert series["table"]["stats"]["min"]["value"] == 2.9
    assert series["basis"] == {"type": "cell", "checked": "server", "by": "router"}
    assert rows["cells"] == [{"row": 0, "column": "value", "value": "6.0"}]
    # Only the cited row travels with a rows citation.
    assert list(rows["table"]["rows"]) == ["0"]


def test_a_derived_table_citation_is_marked_computed_and_checked(calls):
    text = json.dumps({
        "response": "ALBI was -2.78 [cite:c3].",
        "citations": [{
            "kind": "derived", "cite": "c3", "method": "ALBI from the score fetch.",
            "summary": "ALBI.", "result": {"value": -2.78},
            "inputs": [{"cite": "c3", "label": "bilirubin and albumin"}],
        }],
    })
    (citation,) = json.loads(
        server_case.stamp_basis(server_case.expand_citations(text, calls), calls)
    )["citations"]

    assert citation["basis"] == {"type": "computed", "checked": "server", "by": "router"}
    assert citation["table"]["view"] == "score"
    assert [item["input"] for item in citation["table"]["components"]] == ["bilirubin", "albumin"]
