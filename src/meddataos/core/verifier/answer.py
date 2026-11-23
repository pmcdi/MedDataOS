"""One agent reply, judged against the reads of its question.

``Answer`` parses the reply, applies the contract to every citation, and says
what the agent must fix, which the loop sends back until the reply passes.
"""

from __future__ import annotations

import json
import re

from meddataos.core.verifier import schema
from meddataos.core.verifier.contract import Contract
from meddataos.core.verifier.paths import path_key


class Answer:
    """One agent reply, judged against the files opened for its question.

    ``findings`` are the ids of what specialists found for this question. A
    citation the server spliced in for one of them was checked when the
    specialist answered and is not judged again.
    """

    _UNSET = object()
    _FINDING_ID = re.compile(r"([a-z][a-z0-9_]{0,31})\.\d+")

    # -----------------------------------------------------------------------
    # Judgment
    # -----------------------------------------------------------------------

    def __init__(
        self,
        text: str,
        opened: list[dict],
        known: list[dict],
        calls: dict[str, dict] | None = None,
        findings: frozenset[str] = frozenset(),
    ) -> None:
        """Keep the reply and the files opened while it was written."""
        self.text = text.strip()
        self.opened = opened
        self.known = known
        self.calls = calls or {}
        self.findings = findings
        self._problem = self._UNSET

    @property
    def problem(self) -> str | None:
        """What is wrong with this reply, or None when it may be shown."""
        if self._problem is self._UNSET:
            found = self._find_problem()
            self._problem = found
            return found

        return self._problem if isinstance(self._problem, str) else None

    def correction(self) -> str:
        """What the agent is told when this reply is refused."""
        if not self.opened and (self.problem or "").startswith("You opened no files"):
            return f"The server refused that answer. {self.problem}"

        text = (
            f"The server refused that answer. {self.problem} "
            "Send only one JSON object, with no sentences before or after it. "
            f"{Contract.RULE} Example: {json.dumps(Contract.EXAMPLE)}"
        )

        if self.calls:
            shown = ", ".join(
                f"{token}={self._command(call)}" for token, call in self.calls.items()
            )
            text += " Tool results: " + shown

        fetched = {path_key(call.get("web_path")) for call in self.calls.values()}
        names = [
            item["web_path"]
            for item in self.opened
            if path_key(item.get("web_path")) not in fetched
        ]

        if names:
            text += " Opened files: " + ", ".join(names)

        if self.findings:
            text += (
                " Findings you can cite by id, such as [cite:"
                + sorted(self.findings)[0]
                + "]: "
                + ", ".join(sorted(self.findings))
                + ". A finding needs no entry in citations."
            )

        return text

    @staticmethod
    def _command(call: dict) -> str:
        raw = call.get("arguments")
        arguments = raw if isinstance(raw, dict) else {}
        detail = " ".join(f"{key}={value}" for key, value in arguments.items())
        return f"{call.get('tool', '')} {detail}".strip()

    def _find_problem(self) -> str | None:
        if (
            not self.opened
            and not self.findings
            and "[cite:" not in self.text
            and '"citations"' not in self.text
        ):
            if self._is_json_object():
                return (
                    "You opened no files, so answer the user in plain text, not JSON."
                )
            return None

        try:
            parsed = json.loads(self.text)
        except json.JSONDecodeError:
            if "{" in self.text:
                return (
                    "The reply has text outside the JSON object, or the JSON isinvalid."
                )
            if self.findings and not self.opened:
                return (
                    "The reply cites nothing, but specialists returned findings. "
                    "Put [cite:<finding id>] after the words each finding supports."
                )
            return (
                "The reply is plain text, but files were opened, so it must be the "
                "citation JSON."
            )

        if not isinstance(parsed, dict):
            return "The reply must be a JSON object."

        response = parsed.get("response")
        citations = parsed.get("citations")

        if not isinstance(response, str) or not isinstance(citations, list):
            keys = ", ".join(parsed) or "none"
            return (
                'The JSON needs a "response" string and a "citations" list. '
                f"It has keys: {keys}."
            )

        if not Contract.has_marker(response):
            return "The response has no [cite:id] markers."

        trusted = [item for item in citations if self._is_finding(item)]
        own = [item for item in citations if not self._is_finding(item)]

        for item in own:
            if not isinstance(item, dict):
                continue

            handle = item.get("cite")
            call = self.calls.get(handle) if isinstance(handle, str) else None
            shape = schema.shape_problem(item, call)

            if shape:
                return shape

        problems = [
            Contract.citation_problem(item, response, self.calls, self._web_paths())
            for item in own
        ]

        if not trusted and (not own or all(problems)):
            found = "; ".join(item for item in problems if item) or "the list is empty"
            return f"No citation is usable: {found}."

        return (
            self._unknown_finding(response, own)
            or Contract.marker_problem(response)
            or Contract.sentence_numbers_problem(response, self.calls)
        )

    def _is_finding(self, citation: object) -> bool:
        return isinstance(citation, dict) and citation.get("finding") in self.findings

    def problem_of(self, citation: object, response: str) -> str | None:
        """Why this one citation cannot back the reply, or None.

        A reply passes when any citation in it is usable, so one that passed can
        still hold a citation that is not.
        """
        if not isinstance(citation, dict):
            return "a citation is not an object"

        handle = citation.get("cite")
        call = self.calls.get(handle) if isinstance(handle, str) else None
        return schema.shape_problem(citation, call) or Contract.citation_problem(
            citation, response, self.calls, self._web_paths()
        )

    def _unknown_finding(self, response: str, own: list) -> str | None:
        """A marker shaped like a finding id that no specialist returned."""
        specialists = {name.split(".", 1)[0] for name in self.findings}
        named = {
            str(item.get("cite") or item.get("id"))
            for item in own
            if isinstance(item, dict)
        }

        for marker in Contract._MARKER.findall(response):
            match = self._FINDING_ID.fullmatch(marker)

            if (
                match
                and match.group(1) in specialists
                and marker not in self.findings
                and marker not in named
            ):
                return (
                    f"[cite:{marker}] is not a finding. "
                    f"Findings: {', '.join(sorted(self.findings))}."
                )

        return None

    def _is_json_object(self) -> bool:
        try:
            return isinstance(json.loads(self.text), dict)
        except json.JSONDecodeError:
            return False

    def _web_paths(self) -> dict[str, str]:
        """Files a citation may name with web_path.

        A fetched file must use its cite token.
        """
        fetched = {path_key(call.get("web_path")) for call in self.calls.values()}
        source = self.opened or self.known

        if self.findings and not self.opened:
            # Specialists read the files. The router cites their findings, not a
            # file it did not open.
            source = []

        return {
            path_key(item.get("web_path")): item["web_path"]
            for item in source
            if isinstance(item.get("web_path"), str)
            and path_key(item.get("web_path")) not in fetched
        }
