"""UI server: open one folder with datactrl and answer questions about it.

Questions go to the model itself (the harness, the default), or to an external
agent when --agent-url is given.
"""

from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from meddataos.core.model import settings_from_env
from meddataos.server import dispatch, routes
from meddataos.server.state import APP_ROOT, state

load_dotenv(APP_ROOT / ".env")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Connect MCP servers, start the notes ledger, and ask for the patient card.

    The MCP connections close when the server stops.
    """
    dispatch.start_indexes()
    dispatch.fill_card()
    yield
    dispatch.stop_indexes()


app = FastAPI(title="MedDataOS", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(routes.FRONTEND)), name="static")
app.include_router(routes.router)


def log_launch(root: Path, shown: str, port: int) -> None:
    """Print the opened case and where to reach it.

    Flushed so it shows before uvicorn.
    """
    report = state.report or {}
    counts = report.get("counts") or {}

    print(f"Case: {root}", flush=True)
    print(
        "Discovered "
        f"{counts.get('total', 0)} resources "
        f"(notes={counts.get('notes', 0)}, "
        f"tables={counts.get('structured', 0)}, "
        f"dicom={counts.get('dicom', 0)}, "
        f"unknown={counts.get('unknown', 0)})",
        flush=True,
    )

    for ref in report.get("resources") or []:
        modality = ref.get("modality") or "?"
        label = ref.get("label") or ""
        path = ref.get("path") or ""
        print(f"  {modality:<12} {label}  {path}", flush=True)

    for item in report.get("unknown") or []:
        path = item.get("path", "") if isinstance(item, dict) else item
        reason = item.get("reason", "") if isinstance(item, dict) else ""
        print(f"  {'unknown':<12} {path}  {reason}", flush=True)

    if state.harness:
        settings = settings_from_env()
        print(
            f"Harness: {settings.api} API at {settings.base_url} "
            f"model={settings.model or '?'}",
            flush=True,
        )
        tools = dispatch.harness_tools()
        sandbox = tools.sandbox_problem or f"ready ({tools.sandbox.runtime})"
        print(f"Sandbox: {sandbox}", flush=True)

        # Their tools join the list below once connected, after this banner.
        if tools.mcp.servers:
            print(f"MCP: {', '.join(tools.mcp.servers)} (connecting)", flush=True)
    else:
        print(
            f"Agent: {state.agent_url} model={os.getenv('AGENT_MODEL') or '?'}",
            flush=True,
        )

    tool_names = [tool["function"]["name"] for tool in dispatch.router_tools()]
    print(f"Tools: {', '.join(tool_names)}", flush=True)
    print(f"Web UI: http://{shown}:{port}/?patient={quote(root.name)}", flush=True)


def main() -> None:
    """Open the case folder and serve the page."""
    parser = argparse.ArgumentParser(description="MedDataOS")
    parser.add_argument("folder", help="Case folder to open")
    parser.add_argument(
        "--agent-url",
        help=(
            "Use an external agent instead of the harness: its OpenAI-compatible "
            "base URL, the part before /chat/completions"
        ),
    )
    parser.add_argument(
        "--harness",
        action="store_true",
        help=(
            "Talk to the model directly (the default without --agent-url). The "
            "model comes from HARNESS_MODEL_API and the Azure or OpenAI settings."
        ),
    )
    args = parser.parse_args()

    # Asking for both is a contradiction: one thing answers the questions.
    if args.harness and args.agent_url:
        parser.error("pass --agent-url or --harness, not both")

    state.harness = not args.agent_url
    state.agent_url = (args.agent_url or "").rstrip("/") or None
    root = state.open(args.folder)
    app.mount("/case", StaticFiles(directory=str(root)), name="case")

    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    log_launch(root, shown, port)

    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
