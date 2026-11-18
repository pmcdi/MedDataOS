"""Serve the orchestrator at POST /v1/chat/completions.

Pass its address as --agent-url. With specialists registered, each question is
planned, consulted, reconciled and gap-checked in code (see ``flow``). With none,
requests go to the model unchanged, as with the example agent.

    python -m meddataos.orchestrator.serve --port 8001

It reads the model from the same settings as the example agent: OPENAI_MODEL
(with OPENAI_BASE_URL and OPENAI_API_KEY), or the AZURE_OPENAI_* variables.
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from meddataos.orchestrator import flow  # noqa: E402  (after .env is loaded)
from meddataos.orchestrator.model import Model, ModelError, configured  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    model: Model

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        started = time.monotonic()

        try:
            message = flow.respond(body, self.model)
        except ModelError as exc:
            print(f"orchestrator  failed: {exc}", flush=True)
            self._send(exc.status, {"error": {"message": str(exc)}})
            return
        except Exception as exc:  # noqa: BLE001 - report, do not hang the question
            print(f"orchestrator  failed: {exc}", flush=True)
            self._send(500, {"error": {"message": f"The orchestrator failed: {exc}"}})
            return

        calls = [
            call["function"]["name"] for call in message.get("tool_calls") or []
        ]
        did = f"calls {', '.join(calls)}" if calls else "answers"
        print(f"orchestrator  {did} ({time.monotonic() - started:.1f}s)", flush=True)
        self._send(200, {"choices": [{"message": message}]})

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        pass


def main() -> None:
    if not configured():
        raise SystemExit(
            "Set OPENAI_MODEL, or AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, "
            "and AZURE_OPENAI_DEPLOYMENT."
        )

    parser = argparse.ArgumentParser(description="MedDataOS orchestrator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    Handler.model = Model()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    host, port = server.server_address
    print(f"Agent: http://{host}:{port}/v1", flush=True)
    print(f"Launch the server with --agent-url http://{host}:{port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
