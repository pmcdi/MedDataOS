"""What the server checks on a citation of a table: cells, and computed values."""

from __future__ import annotations

import json

import pytest
from meddataos.core.datactrl import Case
from meddataos.core.verifier import Answer, Contract
from meddataos.server.case import case as server_case

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
        "kind": "table",
        "cite": "c1",
        "summary": "AFP 6.0.",
        "cells": [{"row": 0, "column": "VALUE", "value": 6}],
    }
    wrong = {**good, "cells": [{"row": 0, "column": "value", "value": "6.5"}]}
    unseen = {**good, "cells": [{"row": 5, "column": "value", "value": "2.9"}]}
    no_column = {**good, "cells": [{"row": 0, "column": "result"}]}

    assert judge(calls, "AFP was 6.0 [cite:c1].", good) is None
    assert "cell: row 0 value is '6.0', not '6.5'" in judge(
        calls, "AFP was 6.5 [cite:c1].", wrong
    )
    assert "row 5 is not a row that fetch returned" in judge(
        calls, "AFP was 2.9 [cite:c1].", unseen
    )
    assert "row 0 has no column 'result'" in judge(
        calls, "AFP was 6.0 [cite:c1].", no_column
    )


def test_a_table_citation_without_cells_still_passes(calls):
    citation = {
        "kind": "table",
        "cite": "c1",
        "summary": "Lab table.",
        "fields": ["value"],
    }

    assert judge(calls, "The table lists AFP [cite:c1].", citation) is None


def test_a_series_point_is_cited_by_its_file_row(calls):
    citation = {
        "kind": "table",
        "cite": "c2",
        "summary": "AFP 9.6 on 2012-10-09.",
        "cells": [{"row": 6, "value": 9.6}],
    }
    wrong = {**citation, "cells": [{"row": 6, "value": 96}]}

    assert judge(calls, "AFP rose to 9.6 [cite:c2].", citation) is None
    assert "row 6 holds 9.6, not 96" in judge(calls, "AFP rose to 96 [cite:c2].", wrong)


def test_a_superseded_result_can_be_cited_as_what_it_was(calls):
    citation = {
        "kind": "table",
        "cite": "c4",
        "summary": "Preliminary 61, corrected to 68.",
        "cells": [{"row": 3, "value": 61}, {"row": 4, "value": 68}],
    }

    assert (
        judge(
            calls,
            "The preliminary count of 61 was corrected to 68 [cite:c4].",
            citation,
        )
        is None
    )


def test_a_derived_value_has_to_be_one_the_call_returned(calls):
    def derived(value, unit="ng/mL"):
        return {
            "kind": "derived",
            "cite": "c2",
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
            "kind": "derived",
            "cite": "c3",
            "method": "ALBI from the score fetch.",
            "summary": "ALBI.",
            "result": {"value": value, "unit": None},
            "inputs": [{"cite": "c3", "label": "bilirubin and albumin"}],
        }

    assert judge(calls, "ALBI was -2.78 [cite:c3].", albi(-2.78)) is None
    assert judge(calls, "ALBI was -2.8 [cite:c3].", albi(-2.8)) is None
    assert judge(calls, "ALBI was grade 1 [cite:c3].", albi("Grade 1")) is None
    assert "is not a value its call returned" in judge(
        calls, "ALBI was -2.6 [cite:c3].", albi(-2.6)
    )
    assert "is not a value its call returned" in judge(
        calls, "ALBI was grade 2 [cite:c3].", albi("Grade 2")
    )


def test_a_derived_value_from_a_table_needs_its_result(calls):
    citation = {
        "kind": "derived",
        "cite": "c2",
        "method": "From the series.",
        "summary": "AFP rose.",
        "inputs": [{"cite": "c2", "label": "AFP series"}],
    }

    assert "needs result.value" in judge(calls, "AFP rose [cite:c2].", citation)


def test_a_row_number_is_not_a_returned_value(calls):
    # Row 5 holds the nadir. 5 itself is not a value of the series.
    assert Contract.returned(2.9, [calls["c2"]["table"]])
    assert not Contract.returned(5, [calls["c2"]["table"]])


def test_a_delivered_table_citation_carries_checked_cells_and_the_series(calls):
    text = json.dumps(
        {
            "response": "AFP rose to 9.6 [cite:c2]. Baseline AFP was 6.0 [cite:c1].",
            "citations": [
                {
                    "kind": "table",
                    "cite": "c2",
                    "summary": "AFP.",
                    "cells": [{"row": 6}],
                },
                {
                    "kind": "table",
                    "cite": "c1",
                    "summary": "AFP.",
                    "cells": [{"row": 0, "column": "value"}],
                },
            ],
        }
    )
    delivered = json.loads(
        server_case.stamp_basis(server_case.expand_citations(text, calls), calls)
    )
    series, rows = delivered["citations"]

    assert series["cells"] == [
        {
            "row": 6,
            "column": "value",
            "value": 9.6,
            "date": "2012-10-09",
            "unit": "ng/mL",
        }
    ]
    assert [point["value"] for point in series["table"]["points"]] == [6, 2.9, 9.6]
    assert series["table"]["stats"]["min"]["value"] == 2.9
    assert series["basis"] == {"type": "cell", "checked": "server", "by": "router"}
    assert rows["cells"] == [{"row": 0, "column": "value", "value": "6.0"}]
    # Only the cited row travels with a rows citation.
    assert list(rows["table"]["rows"]) == ["0"]


def test_a_derived_table_citation_is_marked_computed_and_checked(calls):
    text = json.dumps(
        {
            "response": "ALBI was -2.78 [cite:c3].",
            "citations": [
                {
                    "kind": "derived",
                    "cite": "c3",
                    "method": "ALBI from the score fetch.",
                    "summary": "ALBI.",
                    "result": {"value": -2.78},
                    "inputs": [{"cite": "c3", "label": "bilirubin and albumin"}],
                }
            ],
        }
    )
    (citation,) = json.loads(
        server_case.stamp_basis(server_case.expand_citations(text, calls), calls)
    )["citations"]

    assert citation["basis"] == {
        "type": "computed",
        "checked": "server",
        "by": "router",
    }
    assert citation["table"]["view"] == "score"
    assert [item["input"] for item in citation["table"]["components"]] == [
        "bilirubin",
        "albumin",
    ]


# ── Numbers in a sentence cited only to table calls ─────────────────────────

AFP_SERIES = {"kind": "table", "cite": "c2", "summary": "AFP series."}


def test_every_number_in_a_table_cited_sentence_was_returned(calls):
    assert judge(calls, "AFP rose from 6.0 to 9.6 ng/mL [cite:c2].", AFP_SERIES) is None

    problem = judge(calls, "AFP rose from 6.0 to 11.4 ng/mL [cite:c2].", AFP_SERIES)
    assert "gives 11.4, which no call it cites returned" in problem


def test_a_written_date_in_a_table_cited_sentence_is_checked(calls):
    assert (
        judge(calls, "AFP was 9.6 ng/mL on October 9, 2012 [cite:c2].", AFP_SERIES)
        is None
    )

    problem = judge(
        calls, "AFP was 9.6 ng/mL on October 19, 2012 [cite:c2].", AFP_SERIES
    )
    assert "October 19, 2012" in problem


def test_a_sentence_that_also_cites_something_else_is_not_judged(calls):
    text = json.dumps(
        {
            "response": "AFP was 9.6 ng/mL [cite:c2] and the tumour measured 3.9 cm [cite:c9].",
            "citations": [AFP_SERIES],
        }
    )
    calls_with_note = {
        **calls,
        "c9": {"tool": "fetch", "modality": "note", "web_path": "/case/n.txt"},
    }

    assert "3.9" not in (Answer(text, [], [], calls_with_note).problem or "")


def test_the_sentence_rule_runs_after_the_citation_checks(calls):
    # The cell is wrong, so that is what the agent is told first.
    wrong_cell = {**AFP_SERIES, "cells": [{"row": 6, "value": 96}]}

    assert "row 6 holds 9.6, not 96" in judge(
        calls, "AFP rose to 96 [cite:c2].", wrong_cell
    )


# ── A sandbox run (exec) cited as a derived value ───────────────────────────

EXEC_RUN = {
    "tool": "exec",
    "arguments": {"command": "python scripts/gtv.py"},
    "file": "exec run",
    "web_path": "",
    "modality": "derived",
    "by": "router",
    "output": {"exit_code": 0, "stdout": "GTVp volume: 2.25 mL\n", "stderr": ""},
}


def derived_from_exec(value):
    return {
        "kind": "derived",
        "cite": "c9",
        "method": "Voxel count of the GTVp mask times voxel volume, in the sandbox.",
        "summary": "GTVp volume.",
        "result": {"value": value, "unit": "mL"},
        "inputs": [{"cite": "c9", "label": "GTVp volume script"}],
    }


def test_a_value_exec_printed_can_be_cited(calls):
    with_exec = {**calls, "c9": EXEC_RUN}
    text = lambda value: json.dumps(
        {  # noqa: E731 - one reply per value
            "response": f"GTVp is {value} mL [cite:c9].",
            "citations": [derived_from_exec(value)],
        }
    )

    assert Answer(text(2.25), [], [], with_exec).problem is None
    problem = Answer(text(2.6), [], [], with_exec).problem
    assert "result 2.6 is not a value its call returned" in problem


def test_a_sentence_cited_to_exec_cannot_add_numbers(calls):
    with_exec = {**calls, "c9": EXEC_RUN}
    text = json.dumps(
        {
            "response": "GTVp is 2.25 mL, about 1.4 cm across [cite:c9].",
            "citations": [derived_from_exec(2.25)],
        }
    )

    assert "gives 1.4" in Answer(text, [], [], with_exec).problem


def test_an_exec_run_can_only_be_cited_as_derived(calls):
    with_exec = {**calls, "c9": EXEC_RUN}
    as_table = {"kind": "table", "cite": "c9", "summary": "GTVp volume."}
    text = json.dumps(
        {"response": "GTVp is 2.25 mL [cite:c9].", "citations": [as_table]}
    )

    assert (
        "came from a call whose kind is 'derived'"
        in Answer(text, [], [], with_exec).problem
    )


def test_an_exec_run_is_recorded_only_for_an_open_question(monkeypatch):
    from meddataos.server.state import state

    monkeypatch.setattr(state, "pending", {"s1"})
    monkeypatch.setattr(state, "calls", {})
    output = {"exit_code": 0, "stdout": "2.25\n", "stderr": ""}

    token = server_case.remember_output(
        "s1", "exec", {"command": "python x.py"}, output
    )

    assert token == "c1"
    assert state.calls["s1"]["c1"]["output"] == output
    assert state.calls["s1"]["c1"]["modality"] == "derived"
    assert server_case.remember_output("closed", "exec", {}, output) is None


# ── A cited volume shows a slice of its series ──────────────────────────────


def test_a_volume_fetch_carries_a_preview_of_its_series():
    from meddataos.server.case import Case

    structures = {"web_path": "/case/rtstruct", "series_kind": "rtstruct"}
    scan = {"web_path": "/case/ct", "series_kind": "acquisition"}

    assert Case._volume_preview(structures, {"roi": " GTVp "}) == {
        "web_path": "/case/rtstruct",
        "overlay": True,
        "roi": "GTVp",
    }
    assert Case._volume_preview(structures, {}) == {
        "web_path": "/case/rtstruct",
        "overlay": True,
    }
    assert Case._volume_preview(scan, {"spacing": [2, 2, 2]}) == {
        "web_path": "/case/ct"
    }
