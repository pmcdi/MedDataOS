"""TotalSegmentator end to end on RADCURE-0006, with a scripted model.

Opt-in: MEDDATAOS_TOTALSEG_E2E=1. It needs the RADCURE-0006 case
(MEDDATAOS_RADCURE_CASE, default ../med-datactrl/data/rawdata/RADCURE-0006),
Docker with the sandbox runtime (HARNESS_SANDBOX_RUNTIME), the totalseg pixi
environment and the "total" weights. It takes about two minutes on a CPU.

The real server and the real MCP server in its container run; only the model is
scripted. The router spawns the dicom subagent, which fetches the planning CT
as a volume, segments the thyroid and C7 with TotalSegmentator, and answers
with a derived citation that the verifier checks against what the server
returned.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from meddataos.harness.mcp_config import Scope, parse_entry, read_entries, trust
from test_harness_mode import output_message
from test_server import (
    get,
    running_server,
    send_chat,
    wait_for_answer,
)

REPO = Path(__file__).resolve().parents[2]
CASE = Path(
    os.getenv("MEDDATAOS_RADCURE_CASE")
    or REPO.parent / "med-datactrl" / "data" / "rawdata" / "RADCURE-0006"
)
SKILL_MARK = "# DICOM and radiology"
SEGMENT = "mcp__totalseg__segment"

pytestmark = pytest.mark.skipif(
    os.getenv("MEDDATAOS_TOTALSEG_E2E") != "1" or not CASE.is_dir(),
    reason="opt-in: MEDDATAOS_TOTALSEG_E2E=1 and the RADCURE-0006 case",
)


def find(value, key: str):
    """The first value under ``key`` anywhere in a nested result."""
    if isinstance(value, dict):
        if key in value:
            return value[key]
        value = list(value.values())

    if isinstance(value, list):
        for item in value:
            found = find(item, key)
            if found is not None:
                return found

    return None


class ScriptedModels(BaseHTTPRequestHandler):
    subagent_tools: list[str] = []
    segment_output: dict = {}

    errors: list[str] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))

        try:
            reply = self.respond(body)
        except Exception:
            # Kept for the assertion message; a crashed handler only says
            # "connection closed" to the server.
            import traceback

            seen = [
                item.get("output", "")[:600]
                for item in body["input"]
                if item.get("type") == "function_call_output"
            ]
            ScriptedModels.errors.append(
                traceback.format_exc() + "\nOUTPUTS: " + json.dumps(seen)
            )
            raise

        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def respond(self, body: dict) -> dict:
        instructions = body.get("instructions", "")
        outputs = [
            json.loads(item["output"])
            for item in body["input"]
            if item.get("type") == "function_call_output"
        ]

        if instructions.startswith("You fill in the patient card"):
            return output_message("{}")

        if SKILL_MARK in instructions:
            return self.subagent(body, instructions, outputs)

        if not outputs:
            return call(
                "r1",
                "spawn_subagent",
                {"agent": "dicom", "task": "Thyroid volume and C7 position?"},
            )

        finding = outputs[-1]["findings"][0]["id"]
        return output_message(
            json.dumps(
                {
                    "response": f"TotalSegmentator measured the thyroid gland [cite:{finding}].",
                    "citations": [],
                }
            )
        )

    def subagent(self, body: dict, instructions: str, outputs: list[dict]) -> dict:
        ScriptedModels.subagent_tools = [tool["name"] for tool in body["tools"]]
        resources = json.loads(
            instructions.split("Case resources: ", 1)[1].split("\n", 1)[0]
        )

        if not outputs:
            ct = next(item for item in resources if "Neck" in str(item.get("label")))
            # The native grid: 512 x 512 x 181 voxels, written straight to the
            # workspace (over the 33.5 M voxels a volume sent inline may have).
            arguments = {"resource_id": ct["id"], "view": "volume"}
            return call("s1", "fetch", arguments)

        if len(outputs) == 1:
            volume = find(outputs[0], "workspace_path")
            return call(
                "s2",
                SEGMENT,
                {"image_path": volume, "structures": ["thyroid_gland", "vertebrae_C7"]},
            )

        fetch_cite, segment = outputs[0]["cite"], outputs[1]
        ScriptedModels.segment_output = segment
        thyroid = segment["result"]["structured"]["structures"]["thyroid_gland"][
            "volume_ml"
        ]
        return output_message(
            json.dumps(
                {
                    "response": f"The thyroid gland measures {thyroid} mL [cite:{segment['cite']}].",
                    "citations": [
                        {
                            "kind": "derived",
                            "cite": segment["cite"],
                            "method": "TotalSegmentator 2.18.0, task total, full resolution.",
                            "summary": "Thyroid gland volume.",
                            "result": {"value": thyroid, "unit": "mL"},
                            "inputs": [
                                {"cite": fetch_cite, "label": "Planning CT volume"},
                                {
                                    "cite": segment["cite"],
                                    "label": "TotalSegmentator segment",
                                },
                            ],
                        }
                    ],
                }
            )
        )

    def log_message(self, *args):
        pass


def wait_for_totalseg(server: dict, seconds: float = 240) -> str:
    """The server connects MCP servers in the background; wait for TotalSegmentator."""
    deadline = time.monotonic() + seconds
    status = ""

    while time.monotonic() < deadline:
        status = get(server, "/api/health").get("mcp", {}).get("totalseg", "")

        if status.startswith(("connected", "refused")) or "gave up" in status:
            return status

        time.sleep(1)

    return status


def call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "output": [
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
            }
        ]
    }


@pytest.fixture(scope="module")
def answered(tmp_path_factory):
    model = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedModels)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    store = tmp_path_factory.mktemp("store")
    # The project registration, approved in this run's own trust store, as
    # `pixi run mcp trust totalseg` would; the developer's approval is not used.
    project = REPO / "configs" / "mcp.json"
    servers = read_entries(project)
    combined = store / "mcp.json"
    combined.write_text(json.dumps({"mcpServers": servers}))
    registered = parse_entry("totalseg", servers["totalseg"], Scope.PROJECT, combined)
    trust(registered, store / "cache" / "mcp_trust.json")

    try:
        with running_server(
            CASE,
            harness=True,
            scratch=store,
            HARNESS_CONFIG=str(REPO / "configs" / "harness.toml"),
            HARNESS_MCP_PROJECT=str(combined),
            HARNESS_LEDGER="off",
            HARNESS_SANDBOX="off",
            HARNESS_MODEL_API="responses",
            AZURE_OPENAI_ENDPOINT="",
            AZURE_OPENAI_REASONING_EFFORT="",
            OPENAI_BASE_URL=f"http://127.0.0.1:{model.server_address[1]}/v1",
            OPENAI_MODEL="scripted",
        ) as server:
            status = wait_for_totalseg(server)
            assert status.startswith("connected"), f"totalseg did not connect: {status}"
            session_id = send_chat(server, "How big is the thyroid gland?")[
                "session_id"
            ]
            final = wait_for_answer(server, session_id, timeout=900)["history"][-1]
            assert not ScriptedModels.errors, ScriptedModels.errors[0]
            yield final
    finally:
        model.shutdown()


def test_the_dicom_subagent_was_offered_totalsegmentator(answered):
    assert SEGMENT in ScriptedModels.subagent_tools


def test_the_measured_volume_passes_the_verifier(answered):
    answer = json.loads(answered["content"])
    structured = ScriptedModels.segment_output["result"]["structured"]
    thyroid = structured["structures"]["thyroid_gland"]

    assert answer["citations"][0]["kind"] == "derived"
    assert thyroid["found"] and 5 < thyroid["volume_ml"] < 40
    assert structured["version"] == "2.18.0"
    # Provenance from the MCP handshake travels with the recorded output.
    assert ScriptedModels.segment_output["result"]["server"]["name"] == "totalseg"


def test_the_c7_centroid_sits_at_the_tumours_level(answered):
    c7 = ScriptedModels.segment_output["result"]["structured"]["structures"][
        "vertebrae_C7"
    ]

    # The RTSTRUCT's GTVp spans z -990 to -1004 mm; its centroid is about -997.
    assert -1004 <= c7["centroid_mm"][2] <= -990
