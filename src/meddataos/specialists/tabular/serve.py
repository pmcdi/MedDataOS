"""Serve the tabular specialist at POST /v1/chat/completions.

The MedDataOS server registers this process as the tabular specialist and runs
the table reads it asks for. The reply is citation JSON with the server's cite
tokens, so every cited cell and computed value is checked by the server.

    python -m meddataos.specialists.tabular.serve --case /path/to/TCGA-DD-A4NF

Gemini is used unless AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, and
AZURE_OPENAI_DEPLOYMENT are set in the environment or in the repo's .env.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import meddataos.specialists.common  # noqa: F401  (loads .env)
from meddataos.datactrl import Case
from meddataos.specialists.common import user_turns
from meddataos.specialists.tabular.agent import next_message


def generate_json(prompt: str) -> Any:
    """One JSON generation with the model the other specialists use."""
    from meddataos.specialists.gemini import GEMINI_MODEL
    from meddataos.specialists.notes import llm

    return llm.generate_json(prompt, model=GEMINI_MODEL)


class Handler(BaseHTTPRequestHandler):
    case: Case
    patient_id: str

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []
        users = user_turns(messages)
        question = users[0] if users else ""
        reads = sum(1 for item in messages if item.get("role") == "tool")
        print(f"tabular  question: {question.splitlines()[0] if question else ''} "
              f"(reads so far: {reads})", flush=True)

        if question == "Fill in the patient card.":
            self._send({"role": "assistant", "content": json.dumps({
                "name": self.patient_id,
                "age": "",
                "sex": "",
                "blood_type": None,
                "allergies": [],
                "conditions": [],
                "data_dates": {},
            })})
            return

        try:
            message = next_message(messages, self.case, generate_json)
            calls = message.get("tool_calls") or []
            print(
                f"tabular  {'asks for ' + str(len(calls)) + ' read(s)' if calls else 'replies'}",
                flush=True,
            )
        except Exception as exc:
            message = {"role": "assistant", "content": f"The tabular agent failed: {exc}"}
            print(f"tabular  failed: {exc}", flush=True)

        self._send(message)

    def _send(self, message: dict) -> None:
        data = json.dumps({"choices": [{"message": message}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        print(f"tabular  {args[0]}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Tabular specialist")
    parser.add_argument("--case", required=True, help="Case folder to open")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8004)
    args = parser.parse_args()

    root = Path(args.case).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"Not a folder: {root}")

    Handler.case = Case.open(root)
    Handler.patient_id = root.name
    tables = Handler.case.report.structured
    print(f"Tabular agent: {len(tables)} table(s) in {root}", flush=True)
    print(f"Patient: {Handler.patient_id}", flush=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    host, port = server.server_address
    print(f"Agent: http://{host}:{port}/v1", flush=True)
    print(
        "Register it beside the others with "
        f"--specialist tabular=http://{host}:{port}/v1. It reads tables through "
        "the MedDataOS server's fetch, so it is not a stand-alone --agent-url.",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
