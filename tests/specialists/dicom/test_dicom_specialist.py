"""DICOM specialist: message translation, payload externalising, tool hand-back,
the sandbox, and the reference index. The model is faked; nothing calls Azure or
the web.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from meddataos.specialists.dicom import agent, sandbox, translate, volumes
from meddataos.specialists.dicom.references import References, Site, extract
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch",
        "description": "Fetch a view of one resource.",
        "parameters": {
            "type": "object",
            "properties": {
                "resource_id": {"type": "string"},
                "view": {"type": "string"},
            },
            "required": ["resource_id"],
        },
    },
}


# ----------------------------------------------------------------------
# Translation
# ----------------------------------------------------------------------


def test_to_input_items_keeps_parallel_tool_calls_in_order() -> None:
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                translate.tool_call("a", "describe", '{"resource_id": "x"}'),
                translate.tool_call("b", "fetch", '{"resource_id": "y"}'),
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "{}"},
        {"role": "tool", "tool_call_id": "b", "content": "[]"},
        {"role": "assistant", "content": "draft"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "The server refused that."}],
        },
    ]
    items = translate.to_input_items(messages)

    assert [i.get("type") or i["role"] for i in items] == [
        "user",
        "function_call",
        "function_call",
        "function_call_output",
        "function_call_output",
        "assistant",
        "user",
    ]
    assert items[1]["call_id"] == "a" and items[2]["name"] == "fetch"
    assert items[-1]["content"] == "The server refused that."
    assert translate.system_prompt(messages) == "rules"


def test_conversation_key_is_stable_as_the_consult_grows() -> None:
    start = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    later = [
        *start,
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "fix"},
    ]
    other = [{"role": "system", "content": "s"}, {"role": "user", "content": "other"}]

    assert translate.conversation_key(start) == translate.conversation_key(later)
    assert translate.conversation_key(start) != translate.conversation_key(other)


def test_completion_shapes() -> None:
    text = translate.completion("m", "hello")
    calls = translate.completion(
        "m", None, [translate.tool_call("c", "fetch", "not json")]
    )

    assert text["choices"][0]["finish_reason"] == "stop"
    assert calls["choices"][0]["finish_reason"] == "tool_calls"
    assert (
        calls["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == "{}"
    )


# ----------------------------------------------------------------------
# Payloads
# ----------------------------------------------------------------------


def test_externalize_saves_payloads_once_and_strips_base64(tmp_path: Path) -> None:
    blob = b"\x1f\x8bfake-nifti"
    result = {
        "status": "ok",
        "cite": "c1",
        "result": {
            "data": {"volume_base64": base64.b64encode(blob).decode(), "meta": {"n": 1}}
        },
    }
    messages = [
        {"role": "user", "content": "q"},
        {"role": "tool", "tool_call_id": "call/1", "content": json.dumps(result)},
    ]

    out = volumes.externalize(messages, tmp_path)
    data = json.loads(out[1]["content"])["result"]["data"]

    assert "volume_base64" not in out[1]["content"]
    assert data["workspace_path"] == "volumes/call_1.nii.gz"
    assert data["meta"] == {"n": 1}
    assert (tmp_path / "volumes" / "call_1.nii.gz").read_bytes() == blob
    assert json.loads(out[1]["content"])["cite"] == "c1"

    # MedDataOS re-sends the history: the second pass writes nothing new.
    again = volumes.externalize(messages, tmp_path)
    assert again[1]["content"] == out[1]["content"]
    assert messages[1]["content"] == json.dumps(result)


# ----------------------------------------------------------------------
# Agent run with a fake model
# ----------------------------------------------------------------------


class ScriptedModel(Model):
    """Returns one scripted output per call and records the input it saw."""

    def __init__(self, steps: list[list[Any]]) -> None:
        self.steps = steps
        self.inputs: list[Any] = []

    async def get_response(self, system_instructions, input, *args, **kwargs):  # type: ignore[override]
        self.inputs.append(input)
        return ModelResponse(output=self.steps.pop(0), usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):  # type: ignore[override]
        raise NotImplementedError


def _call(call_id: str, name: str, arguments: dict) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=json.dumps(arguments),
    )


def _text(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="msg",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


@pytest.fixture
def workspaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sandbox, "WORKSPACES", tmp_path / "workspaces")
    return tmp_path / "workspaces"


def test_meddataos_tool_call_is_handed_back(
    workspaces: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ScriptedModel(
        [
            [
                _call("c1", "fetch", {"resource_id": "dicom:1"}),
                _call("c2", "fetch", {"resource_id": "dicom:2"}),
            ]
        ]
    )
    monkeypatch.setattr(agent, "build_model", lambda: model)
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "q"},
    ]

    reply = asyncio.run(agent.respond(messages, [FETCH_TOOL]))

    assert reply.content is None
    assert [c["id"] for c in reply.tool_calls] == ["c1", "c2"]
    assert json.loads(reply.tool_calls[1]["function"]["arguments"]) == {
        "resource_id": "dicom:2"
    }
    assert len(model.inputs) == 1


@pytest.mark.skipif(
    not (sandbox.SANDBOX_ENV / "bin" / "python").exists(),
    reason="dicom-sandbox pixi environment not installed",
)
def test_sandbox_measures_a_volume_and_keeps_the_workspace(
    workspaces: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import numpy as np
    import SimpleITK as sitk

    array = np.zeros((10, 10, 10), np.uint8)
    array[2:5, 2:6, 2:7] = 1  # 60 voxels of 8 mm^3 = 0.48 cc
    image = sitk.GetImageFromArray(array)
    image.SetSpacing((2.0, 2.0, 2.0))
    path = tmp_path / "mask.nii.gz"
    sitk.WriteImage(image, str(path))
    fetched = {
        "status": "ok",
        "cite": "c9",
        "result": {
            "data": {"volume_base64": base64.b64encode(path.read_bytes()).decode()}
        },
    }
    script = (
        'python -c "import SimpleITK as s, numpy as n; '
        "i=s.ReadImage('volumes/v1.nii.gz'); a=s.GetArrayFromImage(i); "
        'print(int((a==1).sum())*n.prod(i.GetSpacing())/1000)" | tee out/volume.txt'
    )
    model = ScriptedModel(
        [
            [_call("x1", "exec_command", {"cmd": script})],
            [_text("The volume is 0.48 cc [cite:c9].")],
        ]
    )
    monkeypatch.setattr(agent, "build_model", lambda: model)
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "volume?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [translate.tool_call("v1", "fetch", "{}")],
        },
        {"role": "tool", "tool_call_id": "v1", "content": json.dumps(fetched)},
    ]

    reply = asyncio.run(agent.respond(messages, [FETCH_TOOL]))

    assert reply.content == "The volume is 0.48 cc [cite:c9]."
    shell_output = json.dumps(model.inputs[1], default=str)
    assert "0.48" in shell_output
    workspace = workspaces / translate.conversation_key(messages)
    assert (workspace / "out" / "volume.txt").read_text().strip() == "0.48"
    assert (workspace / "volumes" / "v1.nii.gz").exists()


# ----------------------------------------------------------------------
# References
# ----------------------------------------------------------------------

HOME = """<html><head><title>Home</title></head><body><nav>
<a href="/head-neck/cervical-node-mapping/cervical-node-map">Cervical Lymph Node Map</a>
<a href="/chest/mediastinum/mediastinum-lymph-node-map">Mediastinal Lymph Node Map</a>
<a href="/abdomen/liver/li-rads">LI-RADS</a>
<a href="https://elsewhere.org/page">Elsewhere</a>
<a href="/site/themes/x.css">css</a>
</nav></body></html>"""

ARTICLE = """<html><head><title>The Radiology Assistant : Cervical Lymph Node Map</title></head>
<body><nav><a href="/a">A</a><a href="/b">B</a></nav><div class="content">
<h2>Overview</h2><p>The levels were extended to ten in this map of the neck.</p>
<h3>II - Upper jugular</h3><p>Level II receives lymphatics from the face and pharynx.</p>
</div></body></html>"""


@pytest.fixture
def refs(tmp_path: Path) -> tuple[References, list[str]]:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/":
            return httpx.Response(200, text=HOME, headers={"content-type": "text/html"})
        if request.url.path.endswith("cervical-node-map"):
            return httpx.Response(
                200, text=ARTICLE, headers={"content-type": "text/html"}
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    site = Site(name="ra", base_url="https://radiologyassistant.nl/")
    return References([site], cache=tmp_path / "refs", client=client), requested


def test_reference_search_uses_the_local_index(
    refs: tuple[References, list[str]],
) -> None:
    references, requested = refs

    hits = references.search("cervical lymph node levels")["results"]

    assert (
        hits[0]["url"]
        == "https://radiologyassistant.nl/head-neck/cervical-node-mapping/cervical-node-map"
    )
    assert all(
        "elsewhere" not in hit["url"] and "/site/" not in hit["url"] for hit in hits
    )
    assert requested == ["https://radiologyassistant.nl/"]
    references.search("liver")
    assert len(requested) == 1


def test_reference_read_respects_the_allowlist(
    refs: tuple[References, list[str]],
) -> None:
    references, requested = refs

    page = references.read(
        "https://radiologyassistant.nl/head-neck/cervical-node-mapping/cervical-node-map",
        section="upper jugular",
    )
    refused = references.read("https://elsewhere.org/page")

    assert page["title"] == "Cervical Lymph Node Map"
    assert page["text"].startswith("### II - Upper jugular")
    assert "error" in refused
    assert not any("elsewhere" in url for url in requested)


def test_extract_skips_navigation() -> None:
    title, headings, blocks = extract(ARTICLE)

    assert headings == ["Overview", "II - Upper jugular"]
    assert not any(block in ("A", "B", "- A") for block in blocks)
