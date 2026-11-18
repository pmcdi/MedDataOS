"""Serve the DICOM specialist at POST /v1/chat/completions.

    python -m meddataos.specialists.dicom.serve --port 8005

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

# MedDataOS sends this at startup to the --agent-url agent. If this specialist is
# run that way it has no patient card to give, so it answers with an empty one
# instead of running the agent.
PATIENT_CARD_QUESTION = "Fill in the patient card."

# MedDataOS starts its correction turns with this; they are not the question.
CORRECTION_PREFIX = "The server refused"

app = FastAPI(title="MedDataOS DICOM specialist")


def log(message: str) -> None:
    print(f"dicom  {message}", flush=True)


def latest_question(messages: list[dict[str, Any]]) -> str:
    """The most recent user message that is a question, not a correction."""
    user_texts = [
        translate.text_of(message.get("content"))
        for message in messages
        if message.get("role") == "user"
    ]

    for text in reversed(user_texts):
        if not text.startswith(CORRECTION_PREFIX):
            return text.strip()

    return (user_texts[-1] if user_texts else "").strip()


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

    if question == PATIENT_CARD_QUESTION:
        empty_card = {
            "name": "",
            "age": "",
            "sex": "",
            "blood_type": None,
            "allergies": [],
            "conditions": [],
            "data_dates": {},
        }
        return JSONResponse(translate.completion(model, json.dumps(empty_card)))

    consult_label = translate.conversation_key(messages)[:8]
    tool_result_count = sum(1 for message in messages if message.get("role") == "tool")
    log(
        f"consult={consult_label} tool_results={tool_result_count} "
        f"question: {question[:160]}"
    )
    sandbox.remove_idle_workspaces()

    # Any failure is returned as the answer text, so MedDataOS shows it in the
    # chat instead of the consult hanging until its timeout.
    try:
        reply = await agent.respond(messages, tools)
    except Exception as error:  # noqa: BLE001
        traceback.print_exc()
        log(f"consult={consult_label} failed: {error}")
        return JSONResponse(
            translate.completion(model, f"The DICOM specialist failed: {error}")
        )

    if reply.tool_calls:
        tool_names = ", ".join(call["function"]["name"] for call in reply.tool_calls)
        log(f"consult={consult_label} -> MedDataOS tools: {tool_names}")
        return JSONResponse(translate.completion(model, None, reply.tool_calls))

    log(f"consult={consult_label} answer chars={len(reply.content or '')}")
    return JSONResponse(translate.completion(model, reply.content or ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="DICOM specialist (OpenAI Agents SDK)")
    parser.add_argument("--case", default=None, help="Case folder (logged only)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8005)
    args = parser.parse_args()

    if args.case:
        log(f"case: {args.case} (data comes through MedDataOS tools)")
    log(f"model: {agent.model_name() or '<unset>'}  sandbox: {sandbox.SANDBOX_ENV}")
    log(f"agent: http://{args.host}:{args.port}/v1")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
