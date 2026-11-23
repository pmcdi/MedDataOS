"""The notes ledger as harness tools, built in the background when a case opens.

The ledger is the notes specialist's compiled view of a chart: facts extracted
by a model and grounded to exact quotes, a timeline, and the places sources
disagree. ``meddataos.harness.notes`` builds it with its own model settings
(Azure OpenAI when configured, else Gemini), and it is cached per patient.

The tools are for finding things, not for citing them: each fact carries its note
id and character offsets, so the model reads the span with ``fetch`` and cites
that read, which the server checks.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

# A built ledger; typed loosely so the harness does not import the notes
# package until a ledger is actually built.
LedgerBuilder = Callable[[str, Any], Any]

MAX_FACTS = 60
MAX_EVENTS = 120


def _default_builder(patient_id: str, case: Any) -> Any:
    from meddataos.harness.notes.ingest import get_ledger

    return get_ledger(patient_id, case)


class LedgerService:
    """One case's ledger: building in the background, then answering queries."""

    def __init__(
        self,
        build: LedgerBuilder = _default_builder,
        on_done: Callable[[str], None] | None = None,
    ) -> None:
        """``build`` makes the ledger from (patient id, datactrl case).

        ``on_done`` is told the final status (ready, or failed with why), so the
        server can log the end of a build that runs in the background.
        """
        self._build = build
        self.on_done = on_done
        self._lock = threading.Lock()
        self._ledger: Any = None
        self.status = "not started"
        self.note_count = 0

    @property
    def offered(self) -> bool:
        """Whether the case has notes, so the ledger tools are worth offering."""
        return self.note_count > 0

    def start(self, case: Any, patient_id: str) -> threading.Thread | None:
        """Start building in a background thread; returns it, or None with no notes."""
        self.note_count = len(case.report.notes)

        if not self.note_count:
            self.status = "no notes"
            return None

        self.status = "building"
        worker = threading.Thread(
            target=self._run, args=(case, patient_id), name="ledger", daemon=True
        )
        worker.start()
        return worker

    def _run(self, case: Any, patient_id: str) -> None:
        try:
            ledger = self._build(patient_id, case)
        except Exception as exc:  # noqa: BLE001 - a failed build is a status, not a crash
            with self._lock:
                self.status = f"failed: {exc}"
            self._report()
            return

        with self._lock:
            self._ledger = ledger
            self.status = "ready"

        self._report()

    def _report(self) -> None:
        if self.on_done is not None:
            facts = len(getattr(self._ledger, "facts", []) or [])
            detail = f" ({facts} facts)" if self.status == "ready" else ""
            self.on_done(f"{self.status}{detail}")

    def _ready(self) -> Any:
        with self._lock:
            if self._ledger is not None:
                return self._ledger

            status = self.status

        if status == "building":
            raise LedgerNotReady(
                f"The notes ledger is still being built ({self.note_count} notes). "
                "Use notes_search and fetch meanwhile."
            )

        raise LedgerNotReady(f"The notes ledger is unavailable ({status}).")

    # -----------------------------------------------------------------------
    # Tools
    # -----------------------------------------------------------------------

    def query(self, arguments: dict) -> dict:
        """Facts filtered by category, attribute prefix, note, or words."""
        ledger = self._ready()
        limit = max(1, min(int(arguments.get("limit") or 40), MAX_FACTS))
        facts = ledger.query(
            category=arguments.get("category") or None,
            attribute=arguments.get("attribute") or None,
            text=arguments.get("text") or None,
            note_id=arguments.get("note_id") or None,
            limit=limit,
        )
        return {"facts": [fact_row(fact) for fact in facts], "total": len(ledger.facts)}

    def timeline(self, arguments: dict) -> dict:
        """Dated events in order, optionally between two dates (YYYY or YYYY-MM-DD)."""
        ledger = self._ready()
        start = str(arguments.get("from_date") or "")
        end = str(arguments.get("to_date") or "")
        limit = max(1, min(int(arguments.get("limit") or 60), MAX_EVENTS))
        events = []

        for event in ledger.timeline:
            day = str(event.get("date") or "")

            # Compare only as many characters as the bound gives, so "2012"
            # keeps every day of 2012.
            if start and day[: len(start)] < start:
                continue
            if end and day[: len(end)] > end:
                continue

            events.append(event)

        return {"events": events[:limit], "total": len(events)}

    def conflicts(self, arguments: dict) -> dict:
        """Where sources disagree: values, dates, or negations."""
        ledger = self._ready()
        return {"conflicts": ledger.conflicts}


class LedgerNotReady(Exception):
    """The ledger is building, failed, or the case has no notes."""


def fact_row(fact: Any) -> dict:
    """One fact as the model sees it: what, when, and where to read it."""
    return {
        "fact_id": fact.fact_id,
        "attribute": fact.attribute,
        "value": fact.value,
        "unit": fact.unit,
        "date": fact.event_date or fact.note_date,
        "date_kind": fact.date_kind,
        "assertion": fact.assertion,
        "source_type": fact.source_type,
        "note_id": fact.note_id,
        "note_label": fact.note_label,
        "quote": fact.quote,
        "start": fact.char_start,
        "end": fact.char_end,
    }


def tool_definitions() -> list[dict]:
    """The three ledger tools."""

    def tool(name: str, description: str, properties: dict) -> dict:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": properties},
            },
        }

    return [
        tool(
            "ledger_query",
            "Search the notes ledger: facts extracted from the notes, each with its "
            "note id, the exact quote and its character offsets. Filter by "
            "category, attribute prefix (tumor., lab.afp, social_history.), note "
            "id or words. To cite a fact, fetch its note with view=span at those "
            "offsets and cite that read.",
            {
                "category": {"type": "string"},
                "attribute": {"type": "string"},
                "text": {"type": "string"},
                "note_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
        ),
        tool(
            "notes_timeline",
            "Dated clinical events from the notes in order, optionally between "
            "from_date and to_date (YYYY or YYYY-MM-DD). Inferred dates say so.",
            {
                "from_date": {"type": "string"},
                "to_date": {"type": "string"},
                "limit": {"type": "integer"},
            },
        ),
        tool(
            "notes_conflicts",
            "Places the notes disagree: different values for one attribute, a "
            "finding both present and negated, or inconsistent dates. Each names "
            "its attribute and the notes on each side.",
            {},
        ),
    ]
