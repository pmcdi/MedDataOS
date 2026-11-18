"""Tabular specialist with a scripted model: planning, claim checks, citations, needs.

The loop below stands in for the MedDataOS server: it runs each fetch the
specialist asks for and hands the result back with a cite token.
"""

from __future__ import annotations

import json

import pytest

from meddataos.datactrl import Case
from meddataos.specialists.tabular import agent

LABS = """\
collected_at,test,loinc,value,unit,ref_low,ref_high,flag,status
2011-03-10 08:15,AFP,1834-1,6.0,ng/mL,0.0,6.0,,final
2011-03-10 08:15,"Bilirubin, total",1975-2,0.5,mg/dL,0.2,1.2,,final
2011-03-10 08:15,Albumin,1751-7,4.0,g/dL,3.5,5.0,,final
2011-03-10 08:15,INR,6301-6,1.1,,0.9,1.1,,final
2011-08-16 09:10,AFP,1834-1,2.9,ng/mL,0.0,6.0,,final
2012-10-09 08:45,AFP,1834-1,9.6,ng/mL,0.0,6.0,H,final
2013-11-05 08:55,AFP,1834-1,212.0,ng/mL,0.0,6.0,H,final
"""

RECORD = """\
patient_id,Age,Smoking Status,Smoking PY,Stage
P1,87.3,Current,25,I
"""


@pytest.fixture(scope="module")
def case(tmp_path_factory):
    root = tmp_path_factory.mktemp("tabular") / "P1"
    root.mkdir()
    (root / "labs.csv").write_text(LABS)
    (root / "clinical_record.csv").write_text(RECORD)
    return Case.open(root)


def table_id(case: Case, name: str) -> str:
    return next(ref.id for ref in case.report.structured if ref.label == name)


def consult(case: Case, question: str, model, corrections: list[str] | None = None):
    """Run one consult the way the server does. Returns the reply and the prompts."""
    prompts: list[str] = []

    def generate(prompt: str):
        prompts.append(prompt)
        return model(prompt)

    messages = [
        {"role": "system", "content": "You are a specialist."},
        {"role": "user", "content": question},
    ]
    tokens = 0

    for _ in range(6):
        message = agent.next_message(messages, case, generate)
        messages.append(message)

        if not message.get("tool_calls"):
            if corrections:
                messages.append({"role": "user", "content": corrections.pop(0)})
                continue

            return message["content"], prompts, messages

        for call in message["tool_calls"]:
            arguments = json.loads(call["function"]["arguments"])
            resource_id = arguments.pop("resource_id")

            try:
                tokens += 1
                payload = {
                    "status": "ok", "name": "fetch", "cite": f"c{tokens}",
                    "result": case.fetch(resource_id, **arguments),
                }
            except Exception as exc:  # noqa: BLE001 - the server returns tool errors
                tokens -= 1
                payload = {"status": "error", "name": "fetch", "message": str(exc)}

            messages.append({
                "role": "tool", "tool_call_id": call["id"], "content": json.dumps(payload),
            })

    raise AssertionError("the consult did not finish")


def is_plan(prompt: str) -> bool:
    return "Decide which table reads" in prompt


def test_the_catalog_names_shapes_tests_and_scores(case):
    tables = {table["file"]: table for table in agent.catalog(case)}

    assert tables["labs.csv"]["shape"] == "long"
    assert [item["item"] for item in tables["labs.csv"]["items"]][:2] == ["AFP", "Bilirubin, total"]
    assert "child_pugh" in tables["labs.csv"]["scores"]
    assert tables["clinical_record.csv"]["columns"]["Smoking Status"] == "one of: Current"
    only = agent.catalog(case, [table_id(case, "clinical_record.csv")])
    assert [table["file"] for table in only] == ["clinical_record.csv"]


def test_a_series_answer_keeps_checked_claims_and_drops_the_rest(case):
    labs = table_id(case, "labs.csv")

    def model(prompt):
        if is_plan(prompt):
            assert "labs.csv | shape long" in prompt
            assert '"AFP": ng/mL, 4, 2011-03-10 to 2013-11-05' in prompt
            return {"calls": [
                {"table": "labs.csv", "view": "series", "item": "AFP", "from_date": "2011-04-15"},
                {"table": "nope.csv", "view": "series", "item": "AFP"},
                {"table": labs, "view": "pivot"},
            ]}

        assert "[c1] labs.csv" in prompt
        return {"claims": [
            {"text": "After resection AFP was lowest at 2.9 ng/mL on 2011-08-16.",
             "read": "c1", "cells": [{"row": 4}], "computed": False},
            {"text": "The mean AFP was 74.8 ng/mL", "read": "c1", "cells": [{"row": 4}]},
            {"text": "AFP was 9.6 ng/mL on 2012-10-10", "read": "c1", "cells": [{"row": 5}]},
            {"text": "AFP was 9.6 ng/mL on 2012-10-09", "read": "c9", "cells": [{"row": 5}]},
            {"text": "AFP was 212 ng/mL on 2013-11-05", "read": "c1", "cells": [{"row": 99}]},
        ], "not_in_tables": ["The date of resection."]}

    reply, prompts, messages = consult(case, "What was the AFP nadir after resection?", model)
    body = json.loads(reply)
    (citation,) = body["citations"]
    (call,) = messages[2]["tool_calls"]

    # Only the read the catalog allows is asked for.
    assert json.loads(call["function"]["arguments"]) == {
        "resource_id": labs, "view": "series", "item": "AFP", "from_date": "2011-04-15",
    }
    # A wrong number, a wrong date, an unknown read, and an unknown row are all dropped.
    assert body["response"] == (
        "After resection AFP was lowest at 2.9 ng/mL on 2011-08-16 [cite:c1]."
    )
    assert citation == {
        "kind": "table", "cite": "c1", "cells": [{"row": 4}], "fields": ["value"],
        "summary": "After resection AFP was lowest at 2.9 ng/mL on 2011-08-16.",
    }
    assert body["abstentions"] == ["The date of resection."]
    assert body["facts"] == [{
        "key": "lab.afp", "value": 2.9, "cite": "c1", "source_type": "lab_table",
        "label": "labs.csv", "unit": "ng/mL", "date": "2011-08-16",
    }]
    assert len(prompts) == 2


def test_a_computed_value_has_to_be_one_the_series_summary_gives(case):
    def model(prompt):
        if is_plan(prompt):
            return {"calls": [{"table": "labs.csv", "view": "series", "item": "AFP"}]}

        return {"claims": [
            {"text": "AFP rose by 206 ng/mL between 2011-03-10 and 2013-11-05",
             "read": "c1", "cells": [{"row": 0}, {"row": 6}], "computed": True,
             "value": 206, "unit": "ng/mL"},
            {"text": "AFP doubled every 150 days", "read": "c1", "computed": True, "value": 150},
        ]}

    reply, _, _ = consult(case, "How much did AFP change?", model)
    (citation,) = json.loads(reply)["citations"]

    assert citation["kind"] == "derived"
    assert citation["result"] == {"value": 206, "unit": "ng/mL"}
    assert citation["cells"] == [{"row": 0}, {"row": 6}]
    assert citation["inputs"] == [{"cite": "c1", "label": "AFP series"}]
    assert "150" not in reply


def test_an_incomplete_score_becomes_a_need_not_a_guess(case):
    def model(prompt):
        if is_plan(prompt):
            return {"calls": [
                {"table": "labs.csv", "view": "score", "name": "child_pugh", "at": "2011-03-10"},
            ]}

        # A model that states a score anyway is not believed.
        return {"claims": [
            {"text": "The Child-Pugh score was 5, class A", "read": "c1", "computed": True, "value": 5},
        ]}

    reply, _, _ = consult(case, "What was the Child-Pugh score at baseline?", model)
    body = json.loads(reply)

    assert body["citations"] == []
    assert body["response"].startswith("A score could not be computed from the tables alone.")
    assert body["needs"] == [
        {"what": "ascites: none, mild, or moderate to severe", "why": "Child-Pugh score"},
        {"what": "hepatic encephalopathy: none, grade 1 to 2, or grade 3 to 4",
         "why": "Child-Pugh score"},
    ]


def test_context_from_another_specialist_completes_the_score(case):
    question = (
        "What was the Child-Pugh score at baseline?\n\n"
        "Context from other specialists:\nNo ascites (notes.1). Alert and oriented (notes.2)."
    )

    def model(prompt):
        if is_plan(prompt):
            assert "Context from other specialists:\nNo ascites (notes.1)" in prompt
            return {"calls": [{
                "table": "labs.csv", "view": "score", "name": "child_pugh", "at": "2011-03-10",
                "inputs": {"ascites": "none", "encephalopathy": "none"},
            }]}

        return {"claims": [{
            "text": "The Child-Pugh score on 2011-03-10 was 5 points, class A",
            "read": "c1", "computed": True, "value": 5,
        }]}

    reply, _, _ = consult(case, question, model)
    body = json.loads(reply)
    (citation,) = body["citations"]

    assert "needs" not in body
    assert body["response"] == (
        "The Child-Pugh score on 2011-03-10 was 5 points, class A [cite:c1]."
    )
    assert citation["kind"] == "derived"
    assert citation["result"] == {"value": 5, "unit": "points"}
    assert citation["method"].startswith("One to three points each for bilirubin")
    assert citation["inputs"][0] == {
        "cite": "c1", "label": "bilirubin", "detail": "0.5 mg/dL (row 1, 2011-03-10)",
    }
    assert citation["inputs"][3] == {
        "cite": "c1", "label": "ascites",
        "detail": "none (given with the question, not read from the table)",
    }


def test_a_registry_cell_becomes_a_keyed_fact(case):
    def model(prompt):
        if is_plan(prompt):
            return {"calls": [{"table": "clinical_record.csv", "view": "rows"}]}

        return {"claims": [{
            "text": "The registry row records a current smoker with 25 pack-years",
            "read": "c1",
            "cells": [{"row": 0, "column": "smoking status"}, {"row": 0, "column": "Smoking PY"}],
        }]}

    reply, _, _ = consult(case, "Does the patient smoke?", model)
    body = json.loads(reply)
    (citation,) = body["citations"]

    assert citation["cells"] == [
        {"row": 0, "column": "Smoking Status"}, {"row": 0, "column": "Smoking PY"},
    ]
    assert citation["fields"] == ["Smoking PY", "Smoking Status"]
    assert body["facts"] == [
        {"key": "social_history.smoking", "value": "Current", "cite": "c1",
         "source_type": "registry", "label": "clinical_record.csv"},
        {"key": "social_history.pack_years", "value": "25", "cite": "c1",
         "source_type": "registry", "label": "clinical_record.csv"},
    ]


def test_no_plan_is_an_abstention(case):
    reply, prompts, _ = consult(
        case, "What does the MRI show?",
        lambda prompt: {"calls": [], "nothing": "The tables hold no imaging findings."},
    )

    assert json.loads(reply) == {
        "response": "The tables hold no imaging findings.",
        "citations": [],
        "abstentions": ["The tables hold no imaging findings."],
    }
    assert len(prompts) == 1


def test_a_second_round_of_reads_is_allowed_once(case):
    asked = []

    def model(prompt):
        if is_plan(prompt):
            return {"calls": [{"table": "clinical_record.csv", "view": "rows"}]}

        asked.append("more_calls" in prompt)
        more = [{"table": "labs.csv", "view": "series", "item": "AFP"}]
        return {
            "claims": [{"text": "The latest AFP was 212 ng/mL on 2013-11-05",
                        "read": "c2", "cells": [{"row": 6}]}],
            "more_calls": more,
        }

    reply, _, messages = consult(case, "What is the latest AFP?", model)

    assert sum(1 for item in messages if item.get("tool_calls")) == 2
    # The second answer is not offered another round.
    assert asked == [True, False]
    assert json.loads(reply)["citations"][0]["cite"] == "c2"


def test_a_refused_answer_is_tried_again_then_given_up(case):
    def model(prompt):
        if is_plan(prompt):
            return {"calls": [{"table": "labs.csv", "view": "series", "item": "AFP"}]}

        return {"claims": [{"text": "The latest AFP was 212 ng/mL on 2013-11-05",
                            "read": "c1", "cells": [{"row": 6}]}]}

    refusal = "The server refused that answer. citation c1 cell: something is off."
    reply, prompts, _ = consult(
        case, "What is the latest AFP?", model, corrections=[refusal] * 3
    )

    assert "Your previous answer was refused: The server refused that answer." in prompts[2]
    assert reply == (
        "The tabular specialist could not produce a checked answer: "
        "that answer. citation c1 cell: something is off."
    )


def test_numbers_in_units_and_labels_are_not_claims(case):
    read = agent.Read(
        token="c1",
        arguments={"view": "score", "name": "albi", "at": "2011-03-10"},
        result={"view": "score", "data": {
            "value": -2.78, "label": "Grade 1", "at": "2011-03-10",
            "components": [{"input": "albumin", "value": 4, "unit": "g/dL"}],
        }},
    )

    assert agent.unsupported("ALBI on 2011-03-10 was -2.78, grade 1", read) == []
    assert agent.unsupported("Platelets 97 x 10^3/uL", read) == ["97"]
    assert agent.unsupported("ALBI was 2.8 lower than normal", read) == []
    assert agent.unsupported("ALBI was -2.78 on 2011-03-11", read) == ["2011-03-11"]
    # The month of a date the read holds is not a value.
    assert agent.unsupported("3 months later", read) == ["3"]


def test_a_failed_read_is_shown_so_it_can_be_asked_again(case):
    seen = []

    def model(prompt):
        if is_plan(prompt):
            return {"calls": [
                {"table": "labs.csv", "view": "series", "item": "Troponin"},
                {"table": "clinical_record.csv", "view": "rows", "columns": ["age_years"]},
            ]}

        seen.append(prompt)

        if len(seen) == 1:
            return {"claims": [], "more_calls": [
                {"table": "labs.csv", "view": "series", "item": "AFP [ng/mL]"},
            ]}

        return {"claims": [{"text": "The latest AFP was 212 ng/mL on 2013-11-05",
                            "read": "c1", "cells": [{"row": 6}]}]}

    reply, _, _ = consult(case, "What is the latest AFP?", model)

    assert "FAILED {\"view\": \"series\", \"item\": \"Troponin\"}: No item 'Troponin' in this table. Items: AFP" in seen[0]
    # A column that is not in the table is an error, not a column of nulls.
    assert "FAILED {\"view\": \"rows\", \"columns\": [\"age_years\"], \"limit\": 60}: Unknown column 'age_years'; columns: patient_id, Age" in seen[0]
    # A test named with its unit still finds the test.
    assert json.loads(reply)["citations"][0]["cells"] == [{"row": 6}]


def test_an_empty_cell_is_not_cited(case):
    def model(prompt):
        if is_plan(prompt):
            return {"calls": [{"table": "labs.csv", "view": "rows", "limit": 3}]}

        return {"claims": [{"text": "No flag was recorded", "read": "c1",
                            "cells": [{"row": 0, "column": "flag"}]}]}

    reply, _, _ = consult(case, "Was AFP flagged?", model)

    assert json.loads(reply)["citations"] == []


def test_a_registry_cell_is_not_dated_by_another_column_of_its_row(tmp_path):
    root = tmp_path / "P2"
    root.mkdir()
    (root / "clinical_record.csv").write_text("patient_id,Age,RT Start,Last FU\nP2,87.3,2006-01-17,2007-06-28\n")
    record_case = Case.open(root)

    def model(prompt):
        if is_plan(prompt):
            return {"calls": [{"table": "clinical_record.csv", "view": "rows"}]}

        return {"claims": [{"text": "The registry records an age of 87.3", "read": "c1",
                            "cells": [{"row": 0, "column": "Age"}]}]}

    reply, _, _ = consult(record_case, "How old was the patient?", model)

    (fact,) = json.loads(reply)["facts"]
    assert fact["key"] == "demographics.age" and fact["value"] == "87.3"
    assert "date" not in fact
