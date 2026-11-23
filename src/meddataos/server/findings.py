"""What specialists found for the open question, kept by the server.

A specialist's reply is checked once, when it arrives. Each usable citation in it
becomes a finding with an id such as ``notes.2``. The router cites a finding by
that id, and the server puts the checked citation into the delivered answer. The
router never copies quotes, slices, or cells, so what the page shows for a finding
is what the specialist sent and the server checked.

A reply may also say what the specialist's files do not hold (``abstentions``),
what it lacks to answer (``needs``), and the keyed values behind its findings
(``facts``). Those are passed to the router beside the findings.

"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field

from meddataos.core.verifier import Answer, Contract, schema
from meddataos.core.verifier.paths import path_key
from meddataos.server.case import case
from meddataos.server.state import log, short_session, state

MARKER = re.compile(r"\[cite:([^\]]+)\]")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

MAX_ITEMS = 24
MAX_TEXT = 600
MAX_SUMMARY = 300

_FACT_FIELDS = (
    "key",
    "value",
    "unit",
    "date",
    "source_type",
    "assertion",
    "label",
    "normalized",
)


@dataclass
class Reply:
    """A specialist reply: the part the citation check judges, and the rest."""

    body: str
    abstentions: list[str] = field(default_factory=list)
    needs: list[dict] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)


@dataclass
class Finding:
    """One checked citation of a specialist, and the words it supports."""

    id: str
    specialist: str
    text: str
    citation: dict
    facts: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reading a specialist reply
# ---------------------------------------------------------------------------


def parse_reply(text: str) -> Reply:
    """Split a reply into what is checked and what is passed along.

    A JSON reply that cites nothing is judged as its ``response`` text, so a
    specialist can abstain or state a need without inventing a citation.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return Reply(text)

    if not isinstance(parsed, dict) or not isinstance(parsed.get("response"), str):
        return Reply(text)

    reply = Reply(
        text,
        abstentions=_strings(parsed.get("abstentions")),
        needs=_needs(parsed.get("needs")),
        facts=_facts(parsed.get("facts")),
    )
    citations = parsed.get("citations")

    if isinstance(citations, list) and citations:
        reply.body = json.dumps(
            {"response": parsed["response"], "citations": citations},
            ensure_ascii=False,
        )
    elif parsed["response"].strip():
        reply.body = parsed["response"].strip()

    return reply


def _strings(value: object) -> list[str]:
    items = value if isinstance(value, list) else []
    return [
        item.strip()[:MAX_TEXT]
        for item in items
        if isinstance(item, str) and item.strip()
    ][:MAX_ITEMS]


def _needs(value: object) -> list[dict]:
    """What a specialist lacks, as ``{what, why, options}`` with only ``what`` sure."""
    found: list[dict] = []

    for item in value if isinstance(value, list) else []:
        if isinstance(item, str) and item.strip():
            found.append({"what": item.strip()[:MAX_TEXT]})
            continue

        if not isinstance(item, dict) or not isinstance(item.get("what"), str):
            continue

        need: dict = {"what": item["what"].strip()[:MAX_TEXT]}

        if isinstance(item.get("why"), str) and item["why"].strip():
            need["why"] = item["why"].strip()[:MAX_TEXT]

        options = _strings(item.get("options"))

        if options:
            need["options"] = options

        if need["what"]:
            found.append(need)

    return found[:MAX_ITEMS]


def _facts(value: object) -> list[dict]:
    """Keyed values behind the findings, each naming the marker it belongs to."""
    found: list[dict] = []

    for item in value if isinstance(value, list) else []:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("key"), str)
            or not item["key"].strip()
            or item.get("value") is None
            or not isinstance(item.get("cite"), (str, int))
        ):
            continue

        fact = {name: item[name] for name in _FACT_FIELDS if item.get(name) is not None}
        fact["key"] = item["key"].strip().lower()
        fact["cite"] = str(item["cite"])
        found.append(fact)

    return found[: MAX_ITEMS * 4]


# ---------------------------------------------------------------------------
# Accepting a checked reply
# ---------------------------------------------------------------------------


def known(session_id: str) -> frozenset[str]:
    """Ids of the findings held for this question."""
    return frozenset(state.findings.get(session_id) or {})


def accept(
    session_id: str, specialist: str, reply: Reply, text: str, verdict: Answer
) -> dict:
    """Keep each usable citation as a finding and return what the router is told.

    ``text`` passed the citation check as ``verdict``. The returned ``result`` is
    that reply with its markers renamed to finding ids.
    """
    result: dict = {"status": "ok", "name": f"consult_{specialist}", "result": text}
    parsed = _cited(text)

    if parsed is not None and session_id in state.pending:
        _keep(session_id, specialist, reply, parsed, verdict, result)

    if reply.abstentions:
        result["abstentions"] = reply.abstentions

    if reply.needs:
        result["needs"] = reply.needs

    return result


def _cited(text: str) -> dict | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None

    if (
        isinstance(parsed, dict)
        and isinstance(parsed.get("response"), str)
        and isinstance(parsed.get("citations"), list)
    ):
        return parsed

    return None


def _keep(
    session_id: str,
    specialist: str,
    reply: Reply,
    parsed: dict,
    verdict: Answer,
    result: dict,
) -> None:
    store = state.findings.setdefault(session_id, {})
    taken = sum(1 for item in store.values() if item.specialist == specialist)
    files = {path_key(item["web_path"]): item for item in case.files()}
    renamed: dict[str, str] = {}
    kept: list[dict] = []

    for citation in parsed["citations"]:
        problem = verdict.problem_of(citation, parsed["response"])

        if problem:
            log(
                "agent",
                f"session={short_session(session_id)} {specialist} citation "
                f"dropped: {problem}",
            )
            continue

        marker = str(citation.get("cite") or citation.get("id"))

        if marker in renamed:
            continue

        taken += 1
        renamed[marker] = f"{specialist}.{taken}"
        kept.append(
            _checked(citation, renamed[marker], specialist, verdict.calls, files)
        )

    response = MARKER.sub(
        lambda match: f"[cite:{renamed.get(match.group(1), match.group(1))}]",
        parsed["response"],
    )
    facts = [
        {
            **{k: v for k, v in fact.items() if k != "cite"},
            "finding": renamed[fact["cite"]],
        }
        for fact in reply.facts
        if fact["cite"] in renamed
    ]

    for citation in kept:
        store[citation["id"]] = Finding(
            id=citation["id"],
            specialist=specialist,
            text=_sentences(response, citation["id"]),
            citation=citation,
            facts=[fact for fact in facts if fact["finding"] == citation["id"]],
        )

    result["result"] = json.dumps(
        {"response": response, "citations": [_slim(item) for item in kept]},
        ensure_ascii=False,
    )
    result["findings"] = [
        {
            "id": item["id"],
            "text": store[item["id"]].text,
            "kind": item.get("kind"),
            "file": item.get("file"),
            "agent": item.get("agent"),
        }
        for item in kept
    ]

    if facts:
        result["facts"] = facts


def _checked(
    citation: dict,
    finding_id: str,
    specialist: str,
    calls: dict[str, dict],
    files: dict[str, dict],
) -> dict:
    """The citation as the page will get it: filled in, named, and stamped."""
    checked = copy.deepcopy(citation)
    handle = checked.get("cite")
    call = calls.get(handle) if isinstance(handle, str) else None
    modality = ""

    if isinstance(call, dict):
        Contract.apply(checked, call)
        modality = str(call.get("modality") or "")
    else:
        ref = files.get(path_key(checked.get("web_path")))

        if ref:
            checked["web_path"] = ref["web_path"]
            modality = str(ref.get("modality") or "")

        preview = checked.pop("preview", None)

        if not checked.get("dicom") and isinstance(preview, dict):
            record = _dicom_record(str(checked.get("web_path") or ""), preview)

            if record:
                checked["dicom"] = record

    checked["id"] = finding_id
    checked["finding"] = finding_id

    if not checked.get("kind"):
        checked["kind"] = schema.kind_for(call) or "file"

    if not checked.get("agent") and not checked.get("dicom"):
        agent = case._AGENT_BY_MODALITY.get(modality)

        if agent:
            checked["agent"] = agent

    checked["basis"] = Contract.basis(checked, call, specialist)
    return checked


def _dicom_record(web_path: str, preview: dict) -> dict | None:
    """The DICOM slice record a fetched preview gets, built from a specialist's."""
    if not web_path:
        return None

    entry: dict = {"web_path": web_path}

    for name in ("slice_index", "n_slices", "window", "roi"):
        if preview.get(name) is not None:
            entry[name] = preview[name]

    if preview.get("overlay"):
        entry["overlay"] = True

    return {"previews": [entry]}


def _sentences(response: str, finding_id: str) -> str:
    """The sentences of a reply that carry this finding's marker."""
    marker = f"[cite:{finding_id}]"
    found = [part for part in _SENTENCE_END.split(response) if marker in part]
    return " ".join(found).strip()[:MAX_TEXT]


def _slim(citation: dict) -> dict:
    """What the router needs to know about a finding. The rest stays here."""
    slim = {
        name: citation[name]
        for name in ("id", "kind", "file", "web_path", "agent")
        if citation.get(name)
    }
    slim["summary"] = str(citation.get("summary") or "")[:MAX_SUMMARY]
    return slim


# ---------------------------------------------------------------------------
# The router's reply
# ---------------------------------------------------------------------------


def splice(session_id: str, text: str) -> str:
    """Put the checked citation behind every finding the reply cites.

    The reply may be the citation JSON or plain text with markers. A citation the
    router sent for a finding is dropped: only the server's copy is shown. A
    reply that cites no finding comes back as it was, apart from fields only the
    server may set.
    """
    store = state.findings.get(session_id) or {}
    parsed = _response_object(text)
    response = parsed["response"] if parsed is not None else text
    sent = parsed.get("citations") if parsed is not None else None
    citations = sent if isinstance(sent, list) else []
    forged = False

    for item in citations:
        if isinstance(item, dict) and ("finding" in item or "basis" in item):
            item.pop("finding", None)
            item.pop("basis", None)
            forged = True

    used = [name for name in dict.fromkeys(MARKER.findall(response)) if name in store]

    if not used:
        return json.dumps(parsed, ensure_ascii=False) if forged else text

    own = [
        item
        for item in citations
        if not (
            isinstance(item, dict)
            and (item.get("id") in store or item.get("cite") in store)
        )
    ]
    merged = dict(parsed) if parsed is not None else {}
    merged["response"] = response
    merged["citations"] = own + [copy.deepcopy(store[name].citation) for name in used]
    return json.dumps(merged, ensure_ascii=False)


def _response_object(text: str) -> dict | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None

    if isinstance(parsed, dict) and isinstance(parsed.get("response"), str):
        return parsed

    return None


def keep_discrepancies(session_id: str, text: str) -> str:
    """Keep the disagreements between sources that name findings of this question.

    A router may send ``discrepancies``: the values different findings give for
    one attribute. Each value has to name a finding the server holds. The server
    adds where each value came from, and makes sure that finding's citation is in
    the answer so the page can open it.
    """
    parsed = _response_object(text)

    if parsed is None or "discrepancies" not in parsed:
        return text

    store = state.findings.get(session_id) or {}
    citations = parsed.get("citations")
    kept: list[dict] = []

    for item in parsed.pop("discrepancies") or []:
        if not isinstance(item, dict) or not isinstance(item.get("attribute"), str):
            continue

        values = []

        for value in item.get("values") or []:
            finding = (
                store.get(value.get("finding")) if isinstance(value, dict) else None
            )

            if finding is None or value.get("value") is None:
                continue

            entry = {
                name: value[name]
                for name in ("value", "unit", "date", "finding", "side")
                if value.get(name) is not None
            }
            entry["specialist"] = finding.specialist
            entry["agent"] = finding.citation.get("agent")
            entry["file"] = finding.citation.get("file")
            values.append(entry)

        if len({value["finding"] for value in values}) < 2:
            continue

        entry = {"attribute": item["attribute"].strip(), "values": values}

        for name in ("kind", "severity", "rationale"):
            if isinstance(item.get(name), str) and item[name].strip():
                entry[name] = item[name].strip()[:MAX_TEXT]

        if item.get("preferred") in store:
            entry["preferred"] = item["preferred"]

        kept.append(entry)

    if kept and isinstance(citations, list):
        present = {c.get("id") for c in citations if isinstance(c, dict)}

        for entry in kept:
            for value in entry["values"]:
                if value["finding"] not in present:
                    citations.append(copy.deepcopy(store[value["finding"]].citation))
                    present.add(value["finding"])

        parsed["discrepancies"] = kept

    return json.dumps(parsed, ensure_ascii=False)
