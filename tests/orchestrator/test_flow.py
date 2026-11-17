"""The orchestrator's steps with a scripted model, on messages shaped like the server's."""

from __future__ import annotations

import json

import pytest

from meddataos.orchestrator import flow
from meddataos.orchestrator.model import Model

RESOURCES = [
    {"id": "note:1", "modality": "note", "label": "clinic", "web_path": "/case/clinic.txt"},
    {"id": "structured:1", "modality": "structured", "label": "labs.csv", "web_path": "/case/labs.csv"},
    {"id": "dicom:1", "modality": "dicom", "label": "CT", "web_path": "/case/ct"},
]
SYSTEM = {
    "role": "system",
    "content": f"You answer questions.\nCase resources: {json.dumps(RESOURCES)}\nSpecialists: ...",
}


def tool(name: str, description: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": description}}


TOOLS = [
    tool("fetch", "Fetch a resource."),
    tool("consult_notes", "Answers from clinical notes."),
    tool("consult_tabular", "Answers from tables."),
    tool("consult_imaging", "Looks at one DICOM slice."),
]


class Scripted(Model):
    """Replies in order. An exception in the script is raised."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.forwarded: list[dict] = []

    def _next(self, prompt: str):
        self.prompts.append(prompt)
        assert self.replies, f"the model was asked more than scripted: {prompt[:120]}"
        reply = self.replies.pop(0)

        if isinstance(reply, Exception):
            raise reply

        return reply

    def json(self, prompt: str, system: str = ""):
        return self._next(prompt)

    def text(self, prompt: str, system: str = "") -> str:
        return self._next(prompt)

    def forward(self, body: dict) -> dict:
        self.forwarded.append(body)
        return {"role": "assistant", "content": "forwarded"}


def ok(name: str, findings=(), **extra) -> dict:
    return {
        "status": "ok", "name": f"consult_{name}", "result": "...",
        **({"findings": list(findings)} if findings else {}),
        **extra,
    }


def finding(finding_id: str, text: str, file: str = "file") -> dict:
    return {"id": finding_id, "text": f"{text} [cite:{finding_id}].", "kind": "file", "file": file}


def body(question: str, *rounds, refusals=(), history=()) -> dict:
    """A request as the server sends it. A round is a list of (call, result) pairs."""
    messages = [SYSTEM, *history, {"role": "user", "content": question}]

    for calls in rounds:
        messages.append({"role": "assistant", "content": None, "tool_calls": [c for c, _ in calls]})
        messages += [
            {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)}
            for call, result in calls
        ]

    for refusal in refusals:
        messages += [
            {"role": "assistant", "content": "a refused answer"},
            {"role": "user", "content": f"The server refused that answer. {refusal}"},
        ]

    return {"messages": messages, "tools": TOOLS}


def call(call_id: str, specialist: str, question: str, **extra) -> dict:
    return flow._call(call_id, specialist, question, **extra)


def args(message: dict) -> list[tuple[str, str, dict]]:
    return [
        (item["id"], item["function"]["name"], json.loads(item["function"]["arguments"]))
        for item in message["tool_calls"]
    ]


def test_without_specialists_the_request_goes_to_the_model_as_it_came():
    model = Scripted()
    request = {"messages": [SYSTEM, {"role": "user", "content": "Hi"}], "tools": TOOLS[:1]}

    assert flow.respond(request, model) == {"role": "assistant", "content": "forwarded"}
    assert model.forwarded == [request]


def test_the_plan_asks_each_named_specialist_once_in_one_turn():
    model = Scripted({"consults": [
        {"specialist": "notes", "question": "What stage is the glottic cancer?"},
        {"specialist": "consult_tabular", "question": "What dose was delivered by 2006-01-31?"},
        {"specialist": "notes", "question": "A second question for notes"},
        {"specialist": "pathology", "question": "Not a registered specialist"},
        {"specialist": "imaging", "question": ""},
    ]})
    message = flow.respond(body("Stage and dose?"), model)

    assert args(message) == [
        ("plan-notes", "consult_notes", {"question": "What stage is the glottic cancer?"}),
        ("plan-tabular", "consult_tabular", {"question": "What dose was delivered by 2006-01-31?"}),
    ]
    assert "Question: Stage and dose?" in model.prompts[0]
    assert "- tabular: Answers from tables." in model.prompts[0]
    assert "Case files: 1 dicom, 1 note, 1 structured" in model.prompts[0]


def test_a_plan_that_fails_asks_every_specialist_the_question():
    message = flow.respond(body("What happened?"), Scripted(RuntimeError("model down")))

    assert [(name, a["question"]) for _, name, a in args(message)] == [
        ("consult_notes", "What happened?"),
        ("consult_tabular", "What happened?"),
        ("consult_imaging", "What happened?"),
    ]


def test_a_message_that_needs_no_data_is_answered_directly():
    message = flow.respond(body("Hi"), Scripted({"consults": [], "reply": "Hello. Ask me about this patient."}))

    assert message == {"role": "assistant", "content": "Hello. Ask me about this patient."}


def test_earlier_turns_reach_the_plan():
    history = [
        {"role": "user", "content": "How big is the tumour?"},
        {"role": "assistant", "content": json.dumps({
            "response": "It measured 3.2 cm [cite:notes.1].", "citations": [],
        })},
    ]
    model = Scripted({"consults": [{"specialist": "notes", "question": "Where is the 3.2 cm tumour?"}]})
    flow.respond(body("And where is it?", history=history), model)

    assert "Clinician: How big is the tumour?" in model.prompts[0]
    assert "Answer: It measured 3.2 cm ." in model.prompts[0]
    assert "Question: And where is it?" in model.prompts[0]


def test_findings_with_no_gaps_go_straight_to_the_answer():
    first = [(call("plan-notes", "notes", "q"), ok("notes", [finding("notes.1", "Stage I, T1bN0M0")]))]
    model = Scripted("The cancer was stage I, T1bN0M0 [cite:notes.1].")
    message = flow.respond(body("What stage?", first), model)

    assert json.loads(message["content"]) == {
        "response": "The cancer was stage I, T1bN0M0 [cite:notes.1].",
        "citations": [],
    }
    # One model call: the write. No review was needed.
    assert len(model.prompts) == 1
    assert "[notes.1] (notes, file) Stage I, T1bN0M0 ." in model.prompts[0]


def test_a_need_is_routed_to_a_supplier_then_sent_back_as_context():
    question = "What was the Child-Pugh class at baseline?"
    need = {"what": "ascites: none, mild, or moderate to severe", "why": "Child-Pugh score"}
    round_1 = [
        (call("plan-notes", "notes", "What was the Child-Pugh class?"),
         ok("notes", [finding("notes.1", "Child-Pugh A per the clinic note")])),
        (call("plan-tabular", "tabular", "Compute Child-Pugh at 2011-03-10"),
         ok("tabular", needs=[need])),
    ]
    review = Scripted({
        "supply": [
            {"need": 1, "specialist": "notes", "question": "Is ascites documented, and how severe?"},
            {"need": 1, "specialist": "tabular", "question": "A specialist cannot supply itself"},
            {"need": 7, "specialist": "notes", "question": "There is no need 7"},
        ],
        "followups": [{"specialist": "notes", "question": "What was the Child-Pugh class?"}],
    })
    second = flow.respond(body(question, round_1), review)

    assert args(second) == [(
        "supply-tabular-1-0", "consult_notes",
        {"question": "Is ascites documented, and how severe?"},
    )]
    assert "1. tabular needs: ascites: none, mild, or moderate to severe (for Child-Pugh score)" in review.prompts[0]

    round_2 = [(second["tool_calls"][0], ok("notes", [finding("notes.2", "No ascites on exam")]))]
    silent = Scripted()
    third = flow.respond(body(question, round_1, round_2), silent)

    # Code, not a model, sends the answer back to the specialist that needed it.
    assert silent.prompts == []
    # The supplier's finding comes first, then what notes had already found.
    assert args(third) == [(
        "retry-tabular-2", "consult_tabular",
        {"question": "Answer only the part of this question that needed more information "
                     "(Child-Pugh score). The rest is already answered. "
                     "Compute Child-Pugh at 2011-03-10",
         "context": "No ascites on exam [cite:notes.2]. (notes.2)\n"
                    "Child-Pugh A per the clinic note [cite:notes.1]. (notes.1)"},
    )]

    round_3 = [(third["tool_calls"][0], ok("tabular", [finding("tabular.1", "Child-Pugh was 5 points, class A")]))]
    writer = Scripted("Child-Pugh was 5 points, class A [cite:tabular.1].")
    final = flow.respond(body(question, round_1, round_2, round_3), writer)

    assert json.loads(final["content"])["response"] == "Child-Pugh was 5 points, class A [cite:tabular.1]."
    # The need was met, so it is not reported as undetermined.
    assert "Could not be determined" not in writer.prompts[0]


def test_a_need_nobody_could_supply_is_reported_not_retried():
    question = "What was the Child-Pugh class?"
    need = {"what": "ascites grade", "why": "Child-Pugh score"}
    round_1 = [(call("plan-tabular", "tabular", "Compute Child-Pugh"), ok("tabular", needs=[need]))]
    round_2 = [(call("supply-tabular-1-0", "notes", "Is there ascites?"),
                ok("notes", abstentions=["Ascites is not documented."]))]
    message = flow.respond(body(question, round_1, round_2), Scripted({}))

    assert message["content"] == (
        "Not documented (notes): Ascites is not documented. "
        "Could not be determined: tabular needs ascites grade for Child-Pugh score."
    )


def test_an_option_is_chosen_for_a_need_that_lists_them():
    need = {"what": "which series to read", "options": ["dicom:ct", "dicom:mr"]}
    round_1 = [(call("plan-imaging", "imaging", "What does the liver MRI show?"), ok("imaging", needs=[need]))]
    model = Scripted({"choose": [{"need": 1, "option": "dicom:mr"}, {"need": 1, "option": "dicom:made-up"}]})
    message = flow.respond(body("What does the liver MRI show?", round_1), model)

    assert args(message) == [(
        "choose-imaging-1", "consult_imaging",
        {"question": "What does the liver MRI show?", "resource_ids": ["dicom:mr"]},
    )]
    assert "Options: dicom:ct, dicom:mr" in model.prompts[0]


def test_a_follow_up_is_only_asked_when_it_is_new_and_there_is_budget():
    used_up = [
        (call(f"plan-notes-{i}", "notes", f"q{i}"), ok("notes", abstentions=["Weight is not documented."]))
        for i in range(3)
    ]
    review = Scripted({"followups": [
        {"specialist": "notes", "question": "A fourth question for notes"},
        {"specialist": "tabular", "question": "What was the weight on 2011-03-14?"},
    ]})
    message = flow.respond(body("What was the weight?", used_up), review)

    assert args(message) == [(
        "follow-1-1", "consult_tabular", {"question": "What was the weight on 2011-03-14?"},
    )]


def test_consulting_stops_after_three_rounds():
    rounds = [
        [(call(f"plan-notes-{i}", "notes", f"q{i}"), ok("notes", abstentions=["Not documented."]))]
        for i in range(3)
    ]
    message = flow.respond(body("Question", *rounds), Scripted())

    assert "tool_calls" not in message


def test_prose_with_a_number_its_finding_lacks_is_rewritten_then_replaced():
    first = [(call("plan-tabular", "tabular", "q"),
              ok("tabular", [finding("tabular.1", "AFP was lowest at 2.9 ng/mL on 2011-08-16")]))]
    model = Scripted(
        "AFP fell to 2.6 ng/mL on 2011-08-16 [cite:tabular.1].",
        "The AFP nadir was 2.9 ng/mL on 2011-08-17 [cite:tabular.1].",
    )
    message = flow.respond(body("AFP nadir?", first), model)

    assert "The number 2.6 is not in the finding cited in" in model.prompts[1]
    # Twice wrong: the answer is the specialist's own sentence.
    assert json.loads(message["content"])["response"] == (
        "AFP was lowest at 2.9 ng/mL on 2011-08-16 [cite:tabular.1]."
    )


def test_prose_is_repaired_for_form_before_it_is_sent():
    evidence = flow.Evidence(findings=[
        {"id": "notes.1", "text": "Stage I [cite:notes.1].", "specialist": "notes"},
        {"id": "tabular.1", "text": "Dose 51 Gy [cite:tabular.1].", "specialist": "tabular"},
    ])
    text = flow.tidy(
        "```\nStage I disease. [cite:notes.1] The dose was 51 Gy [cite: tabular.1] [cite:notes.1], "
        "as planned [cite:notes.9].\n```",
        evidence,
    )

    assert text == (
        "Stage I disease [cite:notes.1]. The dose was 51 Gy [cite:tabular.1] and [cite:notes.1], "
        "as planned."
    )
    assert flow.prose_problems(text, evidence) == []
    assert flow.prose_problems("Nothing is cited.", evidence) == [
        "The answer cites no finding. Put [cite:<id>] after what each supports."
    ]


def test_units_and_trailing_zeros_are_not_new_numbers():
    evidence = flow.Evidence(findings=[{
        "id": "tabular.1", "specialist": "tabular",
        "text": "Platelets were 97.0 x 10^3/uL and eGFR 46 mL/min/1.73m2 on 2011-03-10 [cite:tabular.1].",
    }])

    assert flow.prose_problems(
        "Platelets were 97 x 10^3/uL with eGFR 46 mL/min/1.73m2 on 2011-03-10 [cite:tabular.1].",
        evidence,
    ) == []
    assert len(flow.prose_problems("Platelets were 79 [cite:tabular.1].", evidence)) == 1


def test_after_two_refusals_the_findings_are_the_answer():
    first = [(call("plan-notes", "notes", "q"), ok("notes", [finding("notes.1", "Stage I")]))]
    model = Scripted()
    message = flow.respond(body("Stage?", first, refusals=["one", "two"]), model)

    assert model.prompts == []
    assert json.loads(message["content"])["response"] == "Stage I [cite:notes.1]."


def test_a_refusal_is_passed_to_the_rewrite():
    first = [(call("plan-notes", "notes", "q"), ok("notes", [finding("notes.1", "Stage I")]))]
    model = Scripted("The cancer was stage I [cite:notes.1].")
    flow.respond(body("Stage?", first, refusals=["The markers sit side by side."]), model)

    assert "Your previous answer was refused: The server refused that answer. The markers sit side by side." in model.prompts[0]


def test_sources_that_disagree_reach_the_writer_and_the_page():
    question = "Does the patient smoke?"
    notes_facts = [
        {"key": "social_history.smoking", "value": "Currently smokes daily", "finding": "notes.1",
         "date": "2005-12-15", "assertion": "present", "source_type": "physician", "label": "consult"},
        {"key": "social_history.smoking", "value": "Never smoked", "finding": "notes.2",
         "date": "2006-01-17", "assertion": "negated", "source_type": "physician", "label": "rt-start"},
    ]
    table_facts = [{"key": "social_history.smoking", "value": "Current", "finding": "tabular.1",
                    "source_type": "registry", "label": "clinical_record.csv"}]
    first = [
        (call("plan-notes", "notes", "q"), ok("notes", [
            finding("notes.1", "The consultation records a current daily smoker"),
            finding("notes.2", "The treatment start note records never smoked"),
        ], facts=notes_facts)),
        (call("plan-tabular", "tabular", "q"), ok("tabular", [
            finding("tabular.1", "The registry row records Smoking Status Current"),
        ], facts=table_facts)),
    ]
    model = Scripted(
        "The patient is most likely a current smoker [cite:notes.1], which the registry row "
        "also records [cite:tabular.1]. One note says never smoked [cite:notes.2]."
    )
    reply = json.loads(flow.respond(body(question, first), model)["content"])
    (found,) = reply["discrepancies"]

    assert found["attribute"] == "social_history.smoking"
    assert [value["finding"] for value in found["values"]] == ["notes.1", "tabular.1", "notes.2"]
    assert found["preferred"] == "notes.1"
    assert 'smoking: "Currently smokes daily" [notes.1] and "Current" [tabular.1] versus "Never smoked" [notes.2]' in model.prompts[0]
    assert "Preferred: [notes.1]" in model.prompts[0]


def test_no_findings_is_said_plainly():
    first = [
        (call("plan-notes", "notes", "q"), ok("notes", abstentions=["The HCV genotype is not documented."])),
        (call("plan-imaging", "imaging", "q"), {"status": "error", "name": "consult_imaging", "message": "imaging model down"}),
    ]
    message = flow.respond(body("HCV genotype?", first), Scripted({}))

    assert message["content"] == (
        "Not documented (notes): The HCV genotype is not documented. "
        "The imaging specialist failed: imaging model down."
    )


@pytest.mark.parametrize("text", ["```json\n{\"a\": 1}\n```", "Here you go: {\"a\": 1} thanks"])
def test_json_is_read_out_of_model_text(text):
    from meddataos.orchestrator.model import parse_json

    assert parse_json(text) == {"a": 1}


def test_a_finding_repeated_by_a_second_consult_is_kept_once():
    first = [(call("plan-tabular", "tabular", "q"), ok(
        "tabular", [finding("tabular.1", "AFP was 2.9 ng/mL on 2011-08-16")],
        needs=[{"what": "ascites grade", "why": "Child-Pugh score"}],
    ))]
    second = [(call("supply-tabular-1-0", "notes", "ascites?"), ok("notes", [finding("notes.1", "No ascites")]))]
    third = [(call("retry-tabular-2", "tabular", "q again", context="No ascites"), ok("tabular", [
        finding("tabular.2", "AFP was 2.9 ng/mL on 2011-08-16"),
        finding("tabular.3", "Child-Pugh was 5 points, class A"),
    ], facts=[
        {"key": "lab.afp", "value": 2.9, "finding": "tabular.2", "date": "2011-08-16"},
    ]))]
    evidence = flow.reconcile(flow.read_turn(body("q", first, second, third)["messages"]))

    assert evidence.ids == ["tabular.1", "notes.1", "tabular.3"]
    assert evidence.needs == []


def test_a_need_already_answered_by_a_finding_goes_back_without_another_consult():
    question = "What was the Child-Pugh score?"
    needs = [
        {"what": "ascites: none, mild, or moderate to severe", "why": "Child-Pugh score"},
        {"what": "hepatic encephalopathy grade", "why": "Child-Pugh score"},
    ]
    round_1 = [
        (call("plan-notes", "notes", "What Child-Pugh class is documented?"), ok("notes", [
            finding("notes.1", "The note records Child-Pugh A"),
            finding("notes.2", "No ascites on exam, alert and oriented"),
        ])),
        (call("plan-tabular", "tabular", "Compute Child-Pugh at 2011-03-10"), ok("tabular", [
            finding("tabular.1", "Bilirubin was 0.5 mg/dL on 2011-03-10"),
        ], needs=needs)),
    ]
    review = Scripted({"answered": [
        {"need": 1, "findings": ["notes.2", "tabular.1", "notes.99"]},
        {"need": 2, "findings": ["notes.2"]},
    ]})
    message = flow.respond(body(question, round_1), review)

    # One call back to tabular, with the notes finding. Its own finding and an
    # unknown id are left out.
    assert args(message) == [(
        "retry-tabular-1", "consult_tabular",
        {"question": "Answer only the part of this question that needed more information "
                     "(Child-Pugh score). The rest is already answered. "
                     "Compute Child-Pugh at 2011-03-10",
         "context": "No ascites on exam, alert and oriented [cite:notes.2]. (notes.2)"},
    )]
    assert "list their ids under \"answered\"" in review.prompts[0]

    round_2 = [(message["tool_calls"][0], ok("tabular", [finding("tabular.2", "Child-Pugh was 5 points, class A")]))]
    evidence = flow.reconcile(flow.read_turn(body(question, round_1, round_2)["messages"]))

    assert evidence.needs == []
    assert evidence.ids == ["notes.1", "notes.2", "tabular.1", "tabular.2"]


def test_a_specialist_waiting_on_a_supplier_is_asked_again_only_once():
    needs = [{"what": "ascites grade", "why": "Child-Pugh score"}, {"what": "age", "why": "FIB-4"}]
    round_1 = [
        (call("plan-notes", "notes", "q"), ok("notes", [finding("notes.1", "No ascites")])),
        (call("plan-tabular", "tabular", "scores"), ok("tabular", needs=needs)),
    ]
    review = Scripted({
        "answered": [{"need": 1, "findings": ["notes.1"]}],
        "supply": [{"need": 2, "specialist": "notes", "question": "How old was the patient?"}],
    })
    message = flow.respond(body("Scores?", round_1), review)

    assert [call_id for call_id, _, _ in args(message)] == ["supply-tabular-1-0"]
