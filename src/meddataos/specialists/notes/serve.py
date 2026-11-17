"""Serve the clinical notes agent at POST /v1/chat/completions.

The new MedDataOS server registers this process as the notes specialist.
It answers from the case folder you pass, using the ledger, timeline,
conflicts, and quote check. The reply is the citation JSON that server expects.

    python -m meddataos.specialists.notes.serve --case /path/to/TCGA-DD-A4NF

Gemini is used unless AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, and
AZURE_OPENAI_DEPLOYMENT are set in the environment or in the repo's .env.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from meddataos.datactrl import Case
from meddataos.specialists.common import latest_request  # also loads .env

from meddataos.specialists.notes.agent import answer_question


def web_path(relative: str) -> str:
    parts = [quote(part) for part in Path(relative).parts if part not in ("", ".")]
    return "/case/" + "/".join(parts)


def _cite_discrepancies(
    response: str,
    conflicts: list,
    files: dict,
    citations: list,
    by_claim: dict,
) -> str:
    """Replace file names in the discrepancy sentence with citation markers.

    A claim already cited keeps its marker. A note that only shows up here
    gets its own card, built from the disagreeing quote.
    """
    cited_notes: dict[str, str] = {}
    for number, rows in by_claim.items():
        if not any(item["id"] == str(number) for item in citations):
            continue
        for row in rows:
            note_id = str(row.get("source_note_id") or row.get("note_id") or "")
            note_id = note_id.split("#", 1)[0]
            if note_id:
                cited_notes.setdefault(note_id, str(number))

    fresh: dict[str, dict] = {}
    labels: list[tuple[str, str]] = []
    for conflict in conflicts:
        for value in conflict.get("values") or []:
            if not isinstance(value, dict):
                continue
            label = str(value.get("note_label") or "")
            note_id = str(value.get("note_id") or "").split("#", 1)[0]
            if not label or label not in response or not note_id:
                continue
            if note_id not in cited_notes and note_id not in fresh and note_id in files:
                fresh[note_id] = value
            if note_id in cited_notes or note_id in fresh:
                labels.append((label, note_id))

    seen_labels: set[str] = set()
    for label, note_id in sorted(labels, key=lambda item: len(item[0]), reverse=True):
        if label in seen_labels:
            continue
        seen_labels.add(label)
        cite_id = cited_notes.get(note_id)
        if cite_id is None:
            cite_id = f"d{list(fresh).index(note_id) + 1}"
            cited_notes[note_id] = cite_id
            file_name, path = files[note_id]
            value = fresh[note_id]
            quote = str(value.get("quote") or value.get("value") or "")
            citations.append({
                "id": cite_id,
                "agent": "clinical_notes",
                "file": file_name,
                "web_path": path,
                "summary": quote[:500],
                "evidence": [{
                    "n": cite_id,
                    "claim": str(value.get("value") or ""),
                    "quote": quote,
                    "label": label,
                    "note_id": note_id,
                    "note_date": value.get("note_date"),
                    "span_url": value.get("span_url"),
                    "derived": value.get("derived") or "native",
                }],
                "conflicts": [
                    item for item in conflicts
                    if any(
                        str(v.get("note_id") or "").split("#", 1)[0] == note_id
                        for v in item.get("values") or []
                        if isinstance(v, dict)
                    )
                ],
            })
        response = response.replace(f"({label})", f"[cite:{cite_id}]")
        response = response.replace(label, f"[cite:{cite_id}]")
    return response


def _touches(conflict: dict, rows: list) -> bool:
    """Whether a conflict involves these evidence rows.

    It does when a row cites one of the conflict's facts, or quotes a passage
    that overlaps one of the conflict's values in the same note.
    """
    values = [v for v in conflict.get("values") or [] if isinstance(v, dict)]
    fact_ids = {v.get("fact_id") for v in values if v.get("fact_id")}

    for row in rows:
        if row.get("fact_id") and row["fact_id"] in fact_ids:
            return True

        note = str(row.get("note_id") or "")
        start, end = row.get("start"), row.get("end")

        if not isinstance(start, int) or not isinstance(end, int):
            continue

        for value in values:
            lo, hi = value.get("char_start"), value.get("char_end")

            if (
                str(value.get("note_id") or "") == note
                and isinstance(lo, int)
                and isinstance(hi, int)
                and lo < end
                and start < hi
            ):
                return True

    return False


def cited_reply(case: Case, result) -> str:
    """Citation JSON for the server, or plain text when nothing was cited.

    Each citation keeps the verified quotes and documented discrepancies
    the page uses for the notes card.
    """
    files = {
        ref.id: (Path(ref.path).name, web_path(ref.path))
        for ref in case.report.notes
    }
    response = result.summary
    citations = []
    seen: set[int] = set()
    by_claim: dict[int, list] = {}
    for evidence in result.evidence:
        number = evidence.get("n")
        if isinstance(number, int):
            by_claim.setdefault(number, []).append(evidence)
    conflicts = [
        item for item in (result.conflicts or [])
        if isinstance(item, dict) and item.get("severity") != "info"
    ]

    for evidence in result.evidence:
        number = evidence.get("n")
        if number in seen or not isinstance(number, int):
            continue
        note_id = str(evidence.get("source_note_id") or evidence.get("note_id") or "")
        found = files.get(note_id.split("#", 1)[0])
        if found is None:
            continue
        marker = f"[{number}]."
        if marker not in response:
            continue
        response = response.replace(marker, f"[cite:{number}].", 1)
        file_name, path = found
        citations.append({
            "id": str(number),
            "agent": "clinical_notes",
            "file": file_name,
            "web_path": path,
            "summary": str(evidence.get("quote") or "")[:500],
            "evidence": by_claim.get(number, []),
            # Only the discrepancies this claim's own quotes are part of. The
            # rest stay in the reply text, not on every card.
            "conflicts": [
                item for item in conflicts if _touches(item, by_claim.get(number, []))
            ],
        })
        seen.add(number)

    response = _cite_discrepancies(response, conflicts, files, citations, by_claim)
    abstentions = [item for item in (result.abstentions or []) if isinstance(item, str)]

    if not citations and not abstentions:
        return response

    reply: dict = {"response": response, "citations": citations}

    if abstentions:
        reply["abstentions"] = abstentions

    # Keyed facts of the claims that became citations, for comparison with
    # what the tables and the images say.
    cited = {item["id"] for item in citations}
    facts = [
        {**{key: value for key, value in fact.items() if key != "n" and value is not None},
         "cite": str(fact["n"])}
        for fact in getattr(result, "facts", None) or []
        if str(fact.get("n")) in cited
    ]

    if facts:
        reply["facts"] = facts

    return json.dumps(reply, ensure_ascii=False)


class Handler(BaseHTTPRequestHandler):
    case: Case
    patient_id: str

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        request = latest_request(body.get("messages") or [])
        question = request.question
        print(f"notes  question: {question}", flush=True)

        if question == "Fill in the patient card.":
            self._send({"choices": [{"message": {
                "role": "assistant",
                "content": json.dumps({
                    "name": self.patient_id,
                    "age": "",
                    "sex": "",
                    "blood_type": None,
                    "allergies": [],
                    "conditions": [],
                    "data_dates": {},
                }),
            }}]})
            return

        try:
            result = answer_question(
                self.patient_id, question, case=self.case, context=request.context
            )
            content = cited_reply(self.case, result)
            print(
                f"notes  mode={result.mode} facts={result.stats.get('facts')} "
                f"chars={len(content)}",
                flush=True,
            )
        except Exception as exc:
            content = f"The notes agent failed: {exc}"
            print(f"notes  failed: {exc}", flush=True)

        self._send({"choices": [{"message": {"role": "assistant", "content": content}}]})

    def _send(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        print(f"notes  {args[0]}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Clinical notes specialist")
    parser.add_argument("--case", required=True, help="Case folder to open")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()

    root = Path(args.case).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"Not a folder: {root}")

    Handler.case = Case.open(root)
    Handler.patient_id = root.name
    notes = Handler.case.report.notes
    print(f"Notes agent: {len(notes)} note(s) in {root}", flush=True)
    print(f"Patient: {Handler.patient_id}", flush=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    host, port = server.server_address
    print(f"Agent: http://{host}:{port}/v1", flush=True)
    print(
        "Notes only: pass that URL as --agent-url when you start the page.",
        flush=True,
    )
    print(
        "Several agents: keep a router as --agent-url and add "
        f"--specialist notes=http://{host}:{port}/v1",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
