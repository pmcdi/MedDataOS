"""One keyed fact store across specialists, and what the sources disagree on.

Each specialist returns the keyed values behind its findings: the notes ledger's
facts, table cells, and (once an imaging agent measures) image measurements. A
key such as ``lab.afp`` or ``social_history.smoking`` means the same thing
whichever specialist sent it, so the values can be compared.

The comparison is the notes specialist's own: ``conflicts.cross_source_conflicts``
groups by attribute, compares normalised values, and ranks sources by
reliability.

"""

from __future__ import annotations

from typing import Any

from meddataos.specialists.notes.conflicts import (
    _NEGATION_TOKENS,
    _content_tokens,
    cross_source_conflicts,
)
from meddataos.specialists.notes.schema import (
    Fact,
    make_fact_id,
    normalize_attribute,
    normalize_value,
)

# Keys specialists use for one thing, brought to the key the table catalog uses.
_KEY_ALIASES = {
    "lab.bilirubin": "lab.bilirubin_total",
    "lab.total_bilirubin": "lab.bilirubin_total",
    "lab.t_bili": "lab.bilirubin_total",
    "lab.tbili": "lab.bilirubin_total",
    "lab.alb": "lab.albumin",
    "lab.cr": "lab.creatinine",
    "lab.creat": "lab.creatinine",
    "lab.plt": "lab.platelets",
    "lab.hgb": "lab.hemoglobin",
    "lab.hb": "lab.hemoglobin",
    "lab.haemoglobin": "lab.hemoglobin",
    "lab.white_blood_cells": "lab.wbc",
    "lab.alpha_fetoprotein": "lab.afp",
    "lab.prothrombin_time": "lab.pt",
    "vital.weight_kg": "vital.weight",
    "vital.height_cm": "vital.height",
    "demographics.age_at_diagnosis": "demographics.age",
    "rt.total_dose": "rt.dose_gy",
    "rt.total_dose_gy": "rt.dose_gy",
    "rt.number_of_fractions": "rt.fractions",
    "performance_status.ecog": "performance.ecog",
}


def canonical_key(key: str) -> str:
    """A fact key as every source should spell it."""
    normalised = normalize_attribute(key)
    return _KEY_ALIASES.get(normalised, normalised)


class FactStore:
    """Keyed facts by the specialist that sent them, each tied to a finding id."""

    def __init__(self) -> None:
        """Start empty."""
        self._by_source: dict[str, list[Fact]] = {}
        self._finding: dict[str, str] = {}

    def add(self, source: str, facts: list[dict[str, Any]]) -> int:
        """Add one specialist's facts. Returns how many were usable."""
        kept = 0

        for item in facts:
            fact = self._fact(item)

            if fact is None or fact.fact_id in self._finding:
                continue

            self._by_source.setdefault(source, []).append(fact)
            self._finding[fact.fact_id] = str(item["finding"])
            kept += 1

        return kept

    @staticmethod
    def _fact(item: dict[str, Any]) -> Fact | None:
        key, finding = item.get("key"), item.get("finding")

        if not isinstance(key, str) or not isinstance(finding, str):
            return None

        if item.get("value") in (None, ""):
            return None

        attribute = canonical_key(key)
        value = str(item["value"]).strip()
        unit = str(item["unit"]).strip() if item.get("unit") else None
        day = str(item["date"]).strip() if item.get("date") else None
        return Fact(
            fact_id=make_fact_id(finding, 0, 0, attribute, f"{value}|{day}"),
            category=attribute.split(".", 1)[0],
            attribute=attribute,
            value=value,
            unit=unit,
            normalized_value=normalize_value(attribute, value, unit),
            event_date=day,
            date_kind="explicit" if day else "unknown",
            relative_time=None,
            assertion=str(item.get("assertion") or "present"),
            source_type=str(item.get("source_type") or "unknown"),
            note_id=finding,
            note_label=str(item.get("label") or finding),
            note_date=day,
            section=None,
            char_start=0,
            char_end=0,
            quote="",
        )

    def __len__(self) -> int:
        return sum(len(facts) for facts in self._by_source.values())

    def discrepancies(self) -> list[dict[str, Any]]:
        """What the sources disagree on, in the shape the server passes to the page.

        Each value names the finding it came from, so the page can open the
        citation behind it.
        """
        found = []

        for conflict in cross_source_conflicts(self._by_source):
            values = [
                {
                    "value": self._shown(value),
                    "date": value.get("note_date"),
                    "finding": self._finding[value["fact_id"]],
                    "side": value["side"],
                }
                for value in conflict["values"]
            ]
            found.append({
                "attribute": conflict["attribute"],
                "kind": conflict["kind"],
                "severity": conflict["severity"],
                "rationale": conflict["rationale"],
                "values": [
                    {key: item for key, item in value.items() if item is not None}
                    for value in values
                ],
                "preferred": self._finding[conflict["preferred"]["fact_id"]],
            })

        return found

    @staticmethod
    def _shown(value: dict[str, Any]) -> str:
        """The value as written. A negated finding that does not say so gets "no"."""
        text = str(value.get("value") or "")

        if value.get("assertion") != "negated" or not text:
            return text

        return text if _content_tokens(text) & _NEGATION_TOKENS else f"no {text}"
