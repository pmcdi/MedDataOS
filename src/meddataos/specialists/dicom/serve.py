"""Serve the DICOM specialist at POST /v1/chat/completions.

    python -m meddataos.specialists.dicom.serve --port 8004

MedDataOS registers it as a specialist (configs/agents.toml). The specialist does
not open the case itself: it asks MedDataOS for data through the tools MedDataOS
sends with each request. ``--case`` is accepted so scripts/stack.py can start it
like the other specialists, and is only logged.

The model is Azure OpenAI (AZURE_OPENAI_*; DICOM_AZURE_DEPLOYMENT overrides the
deployment). The sandbox Python is the ``dicom-sandbox`` pixi environment.
"""

from __future__ import annotations

import argparse
import json
import traceback
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from meddataos.specialists.dicom import agent, sandbox, translate

PATIENT_CARD = "Fill in the patient card."

app = FastAPI(title="MedDataOS DICOM specialist")


def log(message: str) -> None:
    print(f"dicom  {message}", flush=True)


def latest_question(messages: list[dict[str, Any]]) -> str:
    users = [
        translate.text_of(m.get("content")) for m in messages if m.get("role") == "user"
    ]
    for text in reversed(users):
        if not text.startswith("The server refused"):
            return text.strip()
    return (users[-1] if users else "").strip()


@app.get("/health")
@app.get("/v1/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model": agent.model_name(),
        "sandbox": str(sandbox.SANDBOX_ENV),
    }


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": "dicom-specialist", "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    body = await request.json()
    messages: list[dict[str, Any]] = body.get("messages") or []
    tools: list[dict[str, Any]] = body.get("tools") or []
    model = body.get("model") or "dicom-specialist"
    question = latest_question(messages)

    if question == PATIENT_CARD:
        card = {
            "name": "",
            "age": "",
            "sex": "",
            "blood_type": None,
            "allergies": [],
            "conditions": [],
            "data_dates": {},
        }
        return JSONResponse(translate.completion(model, json.dumps(card)))

    key = translate.conversation_key(messages)
    tool_rounds = sum(1 for m in messages if m.get("role") == "tool")
    log(f"consult={key[:8]} tool_results={tool_rounds} question: {question[:160]}")
    sandbox.remove_idle_workspaces()

    try:
        reply = await agent.respond(messages, tools)
    except Exception as exc:  # noqa: BLE001 - report the failure as the answer
        traceback.print_exc()
        log(f"consult={key[:8]} failed: {exc}")
        return JSONResponse(
            translate.completion(model, f"The DICOM specialist failed: {exc}")
        )

    if reply.tool_calls:
        names = ", ".join(c["function"]["name"] for c in reply.tool_calls)
        log(f"consult={key[:8]} -> MedDataOS tools: {names}")
        return JSONResponse(translate.completion(model, None, reply.tool_calls))

    log(f"consult={key[:8]} answer chars={len(reply.content or '')}")
    return JSONResponse(translate.completion(model, reply.content or ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="DICOM specialist (OpenAI Agents SDK)")
    parser.add_argument("--case", default=None, help="Case folder (logged only)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8004)
    args = parser.parse_args()

    if args.case:
        log(f"case: {args.case} (data comes through MedDataOS tools)")
    log(f"model: {agent.model_name() or '<unset>'}  sandbox: {sandbox.SANDBOX_ENV}")
    log(f"agent: http://{args.host}:{args.port}/v1")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
