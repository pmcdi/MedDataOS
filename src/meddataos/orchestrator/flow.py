"""One question, as steps in code: plan, consult, reconcile, gap-check, write.

The MedDataOS server holds the conversation and runs the consults. Each request
it sends carries the whole question so far, so this module keeps no state: it
reads what has happened out of the messages and returns the next step.

    plan       A model writes the sub-question for each specialist. Code sends
               them as consult calls in one turn, so they run at the same time.
    reconcile  Code gathers findings, abstentions, needs and errors, puts the
               specialists' keyed facts in one store, and finds what the
               sources disagree on.
    gap-check  Code decides whether to consult again. A need that another
               specialist has answered goes back to the specialist that raised
               it, as context. For a need nobody has been asked about, a model
               names who can supply it.
    write      A model writes the answer from the findings, citing each by id.
               Code checks the markers and the numbers. If the prose does not
               hold up, the answer is the findings themselves, stitched in code.

A model is asked for judgement: what to ask, who can supply a need, how to say
it. Which step comes next is never the model's call.

"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from meddataos.orchestrator.model import Model
from meddataos.specialists.facts import FactStore

MAX_ROUNDS = 3
MAX_CONSULTS_PER_SPECIALIST = 3
# Findings passed back to a specialist as context when it is asked again.
MAX_CONTEXT_FINDINGS = 12
REFUSAL_PREFIX = "The server refused"
MAX_HISTORY_TURNS = 4
MAX_HISTORY_CHARS = 500

MARKER = re.compile(r"\[cite:([^\]]+)\]")
_ADJACENT = re.compile(r"(\[cite:[^\]]+\])\s*[,;]?\s*(?=\[cite:[^\]]+\])")
_ORPHAN = re.compile(r"([.!?])\s*(\[cite:[^\]]+\])(?=\s|$)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUMBER = re.compile(r"(?<![\w.^/])\d+(?:,\d{3})*(?:\.\d+)?(?![\w^])")

_PLAN_PROMPT = """You route a clinician's question about one patient to specialists. You do not answer it.

Question: {question}
{history}
Specialists:
{roster}

Case files: {inventory}

For each specialist whose files can answer part of the question, write the question to ask it.
- Make it stand alone: name the patient's finding, test, structure or date in full, never "it" or "that".
- Ask only for what that specialist's files hold, and be specific about the values and dates wanted.
- A broad question (a summary, the course over time) goes to every specialist that holds part of it.
- Ask a specialist once here. It can be asked again later if something is missing.
If the message needs no patient data, such as a greeting, return no consults and a short "reply".

Return ONLY JSON: {{"consults": [{{"specialist": "<name>", "question": "<what to ask>"}}], "reply": ""}}"""

_REVIEW_PROMPT = """You check whether specialists have what they need to answer a clinician's question about one patient.

Question: {question}

Findings so far:
{findings}

Not documented, by specialist:
{abstentions}

Open needs (a specialist could not answer without this):
{needs}

Specialists:
{roster}

Already asked:
{asked}

For each open need, do one of these:
- If findings so far already state what is needed, list their ids under "answered". The specialist is then asked again with those findings.
- Otherwise name the specialist whose files can supply it, and the question to ask that specialist, under "supply".
- When the need lists options, pick one option under "choose".
Add a follow-up only when a part of the question is still unanswered, a specialist's files plausibly hold it, and it was not already asked. Do not repeat a question that was asked.

Return ONLY JSON: {{"answered": [{{"need": <number>, "findings": ["<finding id>"]}}],
 "supply": [{{"need": <number>, "specialist": "<name>", "question": "<what to ask>"}}],
 "choose": [{{"need": <number>, "option": "<one of its options>"}}],
 "followups": [{{"specialist": "<name>", "question": "<what to ask>"}}]}}"""

_WRITE_PROMPT = """You write the answer to a clinician's question about one patient, from what specialists found.

Question: {question}

Findings. Cite each by its id:
{findings}
{discrepancies}{abstentions}{unmet}{errors}
Rules:
- Use only the findings. After the words a finding supports, put its marker, written [cite:<id>], such as [cite:{example}].
- Keep every number, unit and date exactly as the finding gives it. Do not add, subtract, convert or round.
- Never put two markers side by side, and never end with a list of markers. A sentence resting on two findings gives each its own clause.
- Lead with the direct answer. Then the support, in short sentences.
- Where the sources disagree, say so: give each value with its marker, and say which is preferred and why.
- Say plainly what is not documented and what could not be determined. Do not guess at it.
- No preamble, no closing summary, no advice beyond what was asked.

Return the answer text only."""


# ---------------------------------------------------------------------------
# Reading the question so far out of the messages
# ---------------------------------------------------------------------------


@dataclass
class Consult:
    """One consult call and what came back."""

    call_id: str
    specialist: str
    question: str
    context: str
    resource_ids: list[str]
    round: int
    result: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Whether the specialist answered."""
        return self.result.get("status") == "ok"


@dataclass
class Turn:
    """The open question: what was asked, earlier turns, consults, and refusals."""

    question: str
    history: list[dict]
    consults: list[Consult]
    rounds: int
    refusals: list[str]
    resources: list[dict]


def read_turn(messages: list[dict]) -> Turn:
    """Find the open question and everything that has happened for it."""
    start = 0

    for index, message in enumerate(messages):
        content = message.get("content")

        if (
            message.get("role") == "user"
            and isinstance(content, str)
            and not content.startswith(REFUSAL_PREFIX)
        ):
            start = index

    question = str(messages[start].get("content") or "") if messages else ""
    history = [
        message
        for message in messages[1:start]
        if message.get("role") in ("user", "assistant")
        and isinstance(message.get("content"), str)
    ]
    consults: list[Consult] = []
    by_id: dict[str, Consult] = {}
    refusals: list[str] = []
    rounds = 0

    for message in messages[start + 1 :]:
        role = message.get("role")

        if role == "assistant" and message.get("tool_calls"):
            rounds += 1

            for call in message["tool_calls"]:
                function = call.get("function") or {}
                name = str(function.get("name") or "")

                if not name.startswith("consult_"):
                    continue

                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}

                raw_ids = arguments.get("resource_ids")
                consult = Consult(
                    call_id=str(call.get("id") or ""),
                    specialist=name.removeprefix("consult_"),
                    question=str(arguments.get("question") or ""),
                    context=str(arguments.get("context") or ""),
                    resource_ids=list(raw_ids) if isinstance(raw_ids, list) else [],
                    round=rounds,
                )
                consults.append(consult)
                by_id[consult.call_id] = consult
        elif role == "tool":
            consult = by_id.get(str(message.get("tool_call_id") or ""))

            if consult is not None:
                try:
                    parsed = json.loads(message.get("content") or "{}")
                except json.JSONDecodeError:
                    parsed = {}

                consult.result = parsed if isinstance(parsed, dict) else {}
        elif role == "user" and str(message.get("content") or "").startswith(
            REFUSAL_PREFIX
        ):
            refusals.append(str(message["content"]))

    return Turn(
        question=question,
        history=history,
        consults=consults,
        rounds=rounds,
        refusals=refusals,
        resources=_resources(messages),
    )


def _resources(messages: list[dict]) -> list[dict]:
    """The case file list the server puts in the system message."""
    system = str(messages[0].get("content") or "") if messages else ""

    if "Case resources: " not in system:
        return []

    try:
        found = json.loads(system.split("Case resources: ", 1)[1].split("\n", 1)[0])
    except json.JSONDecodeError:
        return []

    return [item for item in found if isinstance(item, dict)]


def roster(tools: list[dict]) -> dict[str, str]:
    """Each specialist the server offers, by name, with its description."""
    found: dict[str, str] = {}

    for tool in tools:
        function = tool.get("function") or {}
        name = str(function.get("name") or "")

        if name.startswith("consult_"):
            found[name.removeprefix("consult_")] = str(function.get("description") or "")

    return found


# ---------------------------------------------------------------------------
# Reconcile: what the consults add up to
# ---------------------------------------------------------------------------


@dataclass
class Need:
    """What a specialist lacks, and what has been done about it."""

    specialist: str
    what: str
    why: str
    options: list[str]
    round: int
    # Consults sent to get it, and whether the specialist was asked again since.
    suppliers: list[Consult] = field(default_factory=list)
    retried: bool = False

    @property
    def supplied(self) -> bool:
        """Whether a supplier came back with a finding."""
        return any(consult.result.get("findings") for consult in self.suppliers)


@dataclass
class Evidence:
    """Everything the consults returned, gathered by code."""

    findings: list[dict] = field(default_factory=list)
    abstentions: dict[str, list[str]] = field(default_factory=dict)
    needs: list[Need] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    discrepancies: list[dict] = field(default_factory=list)

    @property
    def ids(self) -> list[str]:
        """Finding ids, in the order they came."""
        return [finding["id"] for finding in self.findings]


def reconcile(turn: Turn) -> Evidence:
    """Gather the consults into findings, gaps, and disagreements between sources."""
    evidence = Evidence()
    facts = FactStore()
    said: set[tuple[str, str]] = set()

    for consult in turn.consults:
        result = consult.result

        if not consult.ok:
            if result:
                evidence.errors[consult.specialist] = str(result.get("message") or "")
            continue

        evidence.errors.pop(consult.specialist, None)

        repeated: set[str] = set()

        for finding in result.get("findings") or []:
            if not isinstance(finding, dict) or not isinstance(finding.get("id"), str):
                continue

            # A specialist asked again may repeat what it already found. The
            # first finding stands, and the repeat is not cited twice.
            words = (
                consult.specialist,
                _norm(MARKER.sub("", str(finding.get("text") or ""))),
            )

            if words in said:
                repeated.add(finding["id"])
                continue

            said.add(words)
            evidence.findings.append({**finding, "specialist": consult.specialist})

        for text in result.get("abstentions") or []:
            listed = evidence.abstentions.setdefault(consult.specialist, [])

            if isinstance(text, str) and text not in listed:
                listed.append(text)

        facts.add(consult.specialist, [
            fact
            for fact in result.get("facts") or []
            if isinstance(fact, dict) and fact.get("finding") not in repeated
        ])

        for item in result.get("needs") or []:
            if isinstance(item, dict) and isinstance(item.get("what"), str):
                evidence.needs.append(Need(
                    specialist=consult.specialist,
                    what=item["what"],
                    why=str(item.get("why") or ""),
                    options=[str(o) for o in item.get("options") or []],
                    round=consult.round,
                ))

    for need in evidence.needs:
        later = [c for c in turn.consults if c.round > need.round]
        need.suppliers = [
            c for c in later if c.call_id.startswith(f"supply-{need.specialist}-")
        ]
        need.retried = any(
            c.specialist == need.specialist
            and c.call_id.startswith(("retry-", "choose-"))
            for c in later
        )

    # A specialist asked again with what it needed no longer has that need.
    latest = {c.specialist: c.round for c in turn.consults}
    evidence.needs = [
        need for need in evidence.needs
        if not (need.retried and need.round < latest.get(need.specialist, 0))
    ]
    known = set(evidence.ids)
    evidence.discrepancies = [
        item for item in facts.discrepancies()
        if all(value["finding"] in known for value in item["values"])
    ]
    return evidence


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------


def respond(body: dict, model: Model) -> dict:
    """The next assistant message for one request from the MedDataOS server.

    Without specialists there is nothing to orchestrate: the request goes to
    the model as it came, tools and all, as the example agent does.
    """
    specialists = roster(body.get("tools") or [])

    if not specialists:
        return model.forward(body)

    turn = read_turn(body.get("messages") or [])

    if turn.rounds == 0:
        return plan(turn, specialists, model)

    evidence = reconcile(turn)

    if not turn.refusals and turn.rounds < MAX_ROUNDS:
        calls = gap_check(turn, evidence, specialists, model)

        if calls:
            return {"role": "assistant", "content": None, "tool_calls": calls}

    return {"role": "assistant", "content": write(turn, evidence, model)}


def _call(call_id: str, specialist: str, question: str, **extra: Any) -> dict:
    arguments = {"question": question, **{k: v for k, v in extra.items() if v}}
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": f"consult_{specialist}",
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def plan(turn: Turn, specialists: dict[str, str], model: Model) -> dict:
    """Decide what to ask each specialist. A plan that fails asks them all."""
    counts: dict[str, int] = {}

    for resource in turn.resources:
        modality = str(resource.get("modality") or "other")
        counts[modality] = counts.get(modality, 0) + 1

    prompt = _PLAN_PROMPT.format(
        question=turn.question,
        history=_history_text(turn.history),
        roster=_roster_text(specialists),
        inventory=", ".join(f"{n} {name}" for name, n in sorted(counts.items()))
        or "unknown",
    )

    try:
        raw = model.json(prompt)
        planned = raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001 - a failed plan falls back to asking everyone
        planned = {"failed": True}

    calls = []
    asked: set[str] = set()

    for item in planned.get("consults") or []:
        if not isinstance(item, dict):
            continue

        name = str(item.get("specialist") or "").removeprefix("consult_")
        question = str(item.get("question") or "").strip()

        if name in specialists and question and name not in asked:
            asked.add(name)
            calls.append(_call(f"plan-{name}", name, question))

    if calls:
        return {"role": "assistant", "content": None, "tool_calls": calls}

    reply = str(planned.get("reply") or "").strip()

    if reply and not planned.get("failed"):
        return {"role": "assistant", "content": reply}

    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            _call(f"plan-{name}", name, turn.question) for name in specialists
        ],
    }


def gap_check(
    turn: Turn, evidence: Evidence, specialists: dict[str, str], model: Model
) -> list[dict]:
    """Consults still worth making, or none.

    First, in code: a specialist whose need another specialist has now answered
    is asked again with that answer as context. Only when there are needs nobody
    has been asked about, or the first round left something undocumented, is a
    model asked whether a finding in hand already answers the need and, if not,
    who could supply it.
    """
    used: dict[str, int] = {}

    for consult in turn.consults:
        used[consult.specialist] = used.get(consult.specialist, 0) + 1

    def room(name: str) -> bool:
        return name in specialists and used.get(name, 0) < MAX_CONSULTS_PER_SPECIALIST

    calls: list[dict] = []
    tag = turn.rounds

    def ask_again(name: str, needs: list[Need], lines: list[str]) -> None:
        """Send a specialist back what it needed, as context, for that part only."""
        if not room(name) or not lines:
            return

        original = next(c for c in turn.consults if c.specialist == name)
        waiting = "; ".join(dict.fromkeys(need.why or need.what for need in needs))
        question = (
            f"Answer only the part of this question that needed more information "
            f"({waiting}). The rest is already answered. {original.question}"
        )
        calls.append(_call(
            f"retry-{name}-{tag}", name, question, context="\n".join(lines)
        ))
        used[name] = used.get(name, 0) + 1

    retry: dict[str, list[Need]] = {}

    for need in evidence.needs:
        if need.supplied and not need.retried:
            retry.setdefault(need.specialist, []).append(need)

    for name, needs in retry.items():
        supplied = []

        for need in needs:
            for consult in need.suppliers:
                for finding in consult.result.get("findings") or []:
                    line = f"{finding.get('text')} ({finding.get('id')})"

                    if line not in supplied:
                        supplied.append(line)

        # Then what the other specialists found earlier: part of what was
        # needed may have been in hand before the supplier was asked.
        for finding in evidence.findings:
            line = f"{finding.get('text')} ({finding['id']})"

            if (
                finding["specialist"] != name
                and line not in supplied
                and len(supplied) < MAX_CONTEXT_FINDINGS
            ):
                supplied.append(line)

        ask_again(name, needs, supplied)

    if calls:
        return calls

    unasked = [need for need in evidence.needs if not need.suppliers and not need.retried]
    first_round_gaps = turn.rounds == 1 and bool(evidence.abstentions)

    if not unasked and not first_round_gaps:
        return []

    try:
        raw = model.json(_REVIEW_PROMPT.format(
            question=turn.question,
            findings=_findings_text(evidence) or "(none)",
            abstentions=_abstentions_text(evidence) or "(none)",
            needs="\n".join(
                f"{index}. {need.specialist} needs: {need.what}"
                + (f" (for {need.why})" if need.why else "")
                + (f" Options: {', '.join(need.options)}" if need.options else "")
                for index, need in enumerate(unasked, start=1)
            ) or "(none)",
            roster=_roster_text(specialists),
            asked="\n".join(
                f"- {consult.specialist}: {consult.question}" for consult in turn.consults
            ),
        ))
        review = raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001 - without a review, answer from what there is
        return []

    already = {(c.specialist, _norm(c.question)) for c in turn.consults}

    def add(call_id: str, name: str, question: str) -> None:
        """A new question for a specialist with budget left, asked once."""
        if not room(name) or not question or (name, _norm(question)) in already:
            return

        already.add((name, _norm(question)))
        calls.append(_call(call_id, name, question))
        used[name] = used.get(name, 0) + 1

    # A need that a finding in hand already answers goes straight back to the
    # specialist that raised it. A specialist that also waits on a supplier is
    # asked again once, after the supplier answers.
    by_id = {finding["id"]: finding for finding in evidence.findings}
    answered: dict[str, tuple[list[Need], list[str]]] = {}
    supplying = {
        need.specialist
        for item in review.get("supply") or []
        if (need := _numbered(unasked, item)) is not None
    }

    for item in review.get("answered") or []:
        need = _numbered(unasked, item)
        ids = item.get("findings") if isinstance(item, dict) else None

        if need is None or not isinstance(ids, list):
            continue

        needs, lines = answered.setdefault(need.specialist, ([], []))

        for finding_id in ids:
            finding = by_id.get(str(finding_id))

            # A specialist's own finding is not news to it.
            if finding is None or finding["specialist"] == need.specialist:
                continue

            line = f"{finding.get('text')} ({finding['id']})"

            if line not in lines:
                lines.append(line)

        if need not in needs:
            needs.append(need)

    for name, (needs, lines) in answered.items():
        if name not in supplying:
            ask_again(name, needs, lines)

    for item in review.get("choose") or []:
        need = _numbered(unasked, item)

        if need is None or str(item.get("option")) not in need.options:
            continue

        original = next(c for c in turn.consults if c.specialist == need.specialist)

        if room(need.specialist):
            calls.append(_call(
                f"choose-{need.specialist}-{tag}",
                need.specialist,
                original.question,
                resource_ids=[str(item["option"])],
            ))
            used[need.specialist] = used.get(need.specialist, 0) + 1

    for index, item in enumerate(review.get("supply") or []):
        need = _numbered(unasked, item)
        name = str(item.get("specialist") or "") if isinstance(item, dict) else ""

        # A specialist cannot supply its own need.
        if need is not None and name != need.specialist:
            add(
                f"supply-{need.specialist}-{tag}-{index}",
                name,
                str(item.get("question") or "").strip(),
            )

    for index, item in enumerate(review.get("followups") or []):
        if isinstance(item, dict):
            add(
                f"follow-{tag}-{index}",
                str(item.get("specialist") or ""),
                str(item.get("question") or "").strip(),
            )

    return calls


def _numbered(needs: list[Need], item: Any) -> Need | None:
    if not isinstance(item, dict):
        return None

    try:
        index = int(item.get("need"))
    except (TypeError, ValueError):
        return None

    return needs[index - 1] if 1 <= index <= len(needs) else None


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def write(turn: Turn, evidence: Evidence, model: Model) -> str:
    """The reply the server checks: prose that cites findings by id.

    The model's prose is used when every marker names a finding and every number
    in a cited sentence is in the finding it cites. One rewrite is asked for.
    After that, or after the server has refused twice, the answer is the
    findings stitched together in code.
    """
    if not evidence.findings:
        return stitched(evidence)

    if len(turn.refusals) >= 2:
        return _reply(stitched(evidence), evidence)

    feedback = (
        f"\n\nYour previous answer was refused: {turn.refusals[-1][:600]}"
        if turn.refusals else ""
    )
    prompt = _WRITE_PROMPT.format(
        question=turn.question,
        findings=_findings_text(evidence),
        discrepancies=_block(
            "Sources disagree:", [_discrepancy_line(item) for item in evidence.discrepancies]
        ),
        abstentions=_block("Not documented:", _abstentions_text(evidence).splitlines()),
        unmet=_block(
            "Could not be determined:",
            [
                f"- {need.specialist} needs {need.what}"
                + (f" for {need.why}" if need.why else "")
                for need in evidence.needs
            ],
        ),
        errors=_block(
            "Specialists that failed:",
            [f"- {name}: {message}" for name, message in evidence.errors.items()],
        ),
        example=evidence.ids[0],
    )

    for attempt in range(2):
        try:
            text = tidy(model.text(prompt + feedback), evidence)
        except Exception:  # noqa: BLE001 - without prose, the findings are the answer
            break

        problems = prose_problems(text, evidence)

        if not problems:
            return _reply(text, evidence)

        feedback = (
            "\n\nYour previous answer had these problems. Write it again:\n- "
            + "\n- ".join(problems[:6])
        )

    return _reply(stitched(evidence), evidence)


def _reply(text: str, evidence: Evidence) -> str:
    body: dict[str, Any] = {"response": text, "citations": []}

    if evidence.discrepancies:
        body["discrepancies"] = evidence.discrepancies

    return json.dumps(body, ensure_ascii=False)


def tidy(text: str, evidence: Evidence) -> str:
    """Repair what the server would refuse for form: fences, stray and paired markers."""
    cleaned = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", text.strip())

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict) and isinstance(parsed.get("response"), str):
        cleaned = parsed["response"]

    known = set(evidence.ids)
    # A marker for a finding that does not exist is dropped with nothing in its place.
    cleaned = MARKER.sub(
        lambda match: match.group(0) if match.group(1).strip() in known else "", cleaned
    )
    cleaned = MARKER.sub(lambda match: f"[cite:{match.group(1).strip()}]", cleaned)
    cleaned = _ORPHAN.sub(r" \2\1", cleaned)
    cleaned = _ADJACENT.sub(r"\1 and ", cleaned)
    return re.sub(r"[ \t]+([.,;])", r"\1", re.sub(r"[ \t]{2,}", " ", cleaned)).strip()


def prose_problems(text: str, evidence: Evidence) -> list[str]:
    """Why the prose cannot be sent: no citation, or a number its finding lacks."""
    by_id = {finding["id"]: str(finding.get("text") or "") for finding in evidence.findings}

    if not MARKER.search(text):
        return ["The answer cites no finding. Put [cite:<id>] after what each supports."]

    problems = []

    for sentence in _SENTENCE_END.split(text):
        cited = [name for name in MARKER.findall(sentence) if name in by_id]

        if not cited:
            continue

        source = " ".join(by_id[name] for name in cited)
        have = {_plain(n) for n in _NUMBER.findall(_DATE.sub(" ", MARKER.sub(" ", source)))}
        days = set(_DATE.findall(source))
        bare = MARKER.sub(" ", sentence)

        for day in _DATE.findall(bare):
            if day not in days:
                problems.append(
                    f"The date {day} is not in the finding cited in: {sentence.strip()}"
                )

        for number in _NUMBER.findall(_DATE.sub(" ", bare)):
            if _plain(number) not in have:
                problems.append(
                    f"The number {number} is not in the finding cited in: "
                    f"{sentence.strip()}"
                )

    return problems


def _plain(number: str) -> str:
    text = number.replace(",", "")
    return text.rstrip("0").rstrip(".") if "." in text else text


def stitched(evidence: Evidence) -> str:
    """The answer built in code from the findings, when prose is not to be trusted.

    Every sentence is a specialist's own, with its marker where the specialist
    put it, so the server accepts it as it is.
    """
    parts = [str(finding.get("text") or "").strip() for finding in evidence.findings]
    # Two findings cited in one sentence share that sentence. It is said once.
    parts = [part for part in dict.fromkeys(parts) if part]

    for item in evidence.discrepancies:
        sides: dict[int, list[str]] = {}

        for value in item["values"]:
            sides.setdefault(value.get("side", 0), []).append(
                f"{value['value']} [cite:{value['finding']}]"
            )

        parts.append(
            f"Sources disagree on {_readable(item['attribute'])}: "
            + " versus ".join(" and ".join(side) for side in sides.values())
            + f". {item.get('rationale', '')}".rstrip()
        )

    for name, listed in evidence.abstentions.items():
        parts.append(
            f"Not documented ({name}): " + "; ".join(t.rstrip(".") for t in listed) + "."
        )

    for need in evidence.needs:
        parts.append(
            f"Could not be determined: {need.specialist} needs {need.what.rstrip('.')}"
            + (f" for {need.why.rstrip('.')}" if need.why else "")
            + "."
        )

    for name, message in evidence.errors.items():
        parts.append(f"The {name} specialist failed: {message.rstrip('.')}.")

    if not parts:
        return "No specialist returned a finding for this question."

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Prompt text
# ---------------------------------------------------------------------------


def _readable(attribute: str) -> str:
    return attribute.split(".", 1)[-1].replace("_", " ")


def _block(title: str, lines: list[str]) -> str:
    lines = [line for line in lines if line.strip()]
    return f"\n{title}\n" + "\n".join(lines) + "\n" if lines else ""


def _roster_text(specialists: dict[str, str]) -> str:
    return "\n".join(f"- {name}: {text}" for name, text in specialists.items())


def _findings_text(evidence: Evidence) -> str:
    return "\n".join(
        f"[{finding['id']}] ({finding['specialist']}"
        + (f", {finding['file']}" if finding.get("file") else "")
        + f") {MARKER.sub('', str(finding.get('text') or '')).strip()}"
        for finding in evidence.findings
    )


def _abstentions_text(evidence: Evidence) -> str:
    return "\n".join(
        f"- {name}: " + "; ".join(listed)
        for name, listed in evidence.abstentions.items()
        if listed
    )


def _discrepancy_line(item: dict) -> str:
    sides: dict[int, list[str]] = {}

    for value in item["values"]:
        sides.setdefault(value.get("side", 0), []).append(
            f"\"{value['value']}\" [{value['finding']}]"
        )

    return (
        f"- {_readable(item['attribute'])}: "
        + " versus ".join(" and ".join(side) for side in sides.values())
        + f". Preferred: [{item.get('preferred')}]. {item.get('rationale', '')}"
    )


def _history_text(history: list[dict]) -> str:
    """Earlier turns, so the plan can resolve "it" and "that"."""
    lines = []

    for message in history[-2 * MAX_HISTORY_TURNS :]:
        content = str(message.get("content") or "")

        if message.get("role") == "assistant":
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                parsed = None

            if isinstance(parsed, dict) and isinstance(parsed.get("response"), str):
                content = MARKER.sub("", parsed["response"])

        who = "Clinician" if message.get("role") == "user" else "Answer"
        lines.append(f"{who}: {' '.join(content.split())[:MAX_HISTORY_CHARS]}")

    return "\nEarlier in this chat:\n" + "\n".join(lines) + "\n" if lines else ""
