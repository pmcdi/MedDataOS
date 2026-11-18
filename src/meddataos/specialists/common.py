"""Paths, environment, and case lookup shared by the specialists."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from meddataos.datactrl import Case

REPO = Path(__file__).resolve().parents[3]

# Local cases live under <repo>/data/<patient_id> unless MEDDATA_CASES says otherwise.
CASES_DIR = Path(os.getenv("MEDDATA_CASES", REPO / "data")).expanduser()

# Disk caches (note ledgers). Ignored by git.
CACHE_DIR = Path(os.getenv("SPECIALISTS_CACHE", REPO / ".cache" / "specialists")).expanduser()

load_dotenv(REPO / ".env")

_LOCK = threading.Lock()
_CASES: dict[str, Case] = {}


def case_root(patient_id: str) -> Path:
    return CASES_DIR / patient_id


def has_case(patient_id: str) -> bool:
    return case_root(patient_id).is_dir()


def get_case(patient_id: str) -> Case:
    """Return a cached Case for ``patient_id``, opening it on first use."""
    if not has_case(patient_id):
        raise KeyError(f"No case folder for {patient_id!r} under {CASES_DIR}")
    with _LOCK:
        case = _CASES.get(patient_id)
        if case is None:
            case = _CASES[patient_id] = Case.open(case_root(patient_id))
        return case


# The lines the server adds below a consult's question. They match
# CONTEXT_HEADING and RESOURCES_HEADING in meddataos.server.dispatch.
CONTEXT_HEADING = "Context from other specialists:"
RESOURCES_HEADING = "Use these resources:"
REFUSAL_PREFIX = "The server refused"


@dataclass
class Request:
    """One consult: the question, what other specialists found, and files to use."""

    question: str
    context: str = ""
    resource_ids: list[str] = field(default_factory=list)


def parse_request(text: str) -> Request:
    """Split a consult message into its question, context, and resource ids."""
    body = text.strip()
    resource_ids: list[str] = []
    marker = f"\n\n{RESOURCES_HEADING}"

    if marker in body:
        body, _, listed = body.rpartition(marker)
        resource_ids = [part.strip() for part in listed.split(",") if part.strip()]

    question, found, context = body.partition(f"\n\n{CONTEXT_HEADING}")
    return Request(
        question=question.strip(),
        context=context.strip() if found else "",
        resource_ids=resource_ids,
    )


def user_turns(messages: list) -> list[str]:
    """The text of each user message, in order."""
    return [
        message["content"]
        for message in messages
        if isinstance(message, dict)
        and message.get("role") == "user"
        and isinstance(message.get("content"), str)
    ]


def latest_request(messages: list) -> Request:
    """The latest question, skipping a citation-correction turn.

    The page sends the whole chat. A correction from the server is appended
    after the question, so the question is the last user turn that is not one.
    """
    users = user_turns(messages)

    for text in reversed(users):
        if not text.startswith(REFUSAL_PREFIX):
            return parse_request(text)

    return parse_request(users[-1] if users else "")
