"""Retries for failures that pass, and a context overflow told apart from a refusal.

A local endpoint plays a script of replies; ``model.sleep`` is replaced so the
waits are recorded, not slept.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from meddataos.core import model
from meddataos.core.loop import ToolLoop
from meddataos.core.model import ChatCompletionsClient, ModelError, _retry_after

ANSWER = (200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}, {})
HISTORY = [{"role": "user", "content": "hi"}]


class Scripted(BaseHTTPRequestHandler):
    """Answers each request with the next (status, body, headers) in the script."""

    script: list[tuple[int, dict, dict]] = []
    delay = 0.0
    count = 0

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        Scripted.count += 1
        time.sleep(Scripted.delay)
        status, body, headers = Scripted.script.pop(0)
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))

        for name, value in headers.items():
            self.send_header(name, value)

        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def waits(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr(model, "sleep", recorded.append)
    monkeypatch.delenv("HARNESS_MODEL_RETRIES", raising=False)
    return recorded


@pytest.fixture
def client():
    Scripted.script, Scripted.delay, Scripted.count = [], 0.0, 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield ChatCompletionsClient(
        f"http://127.0.0.1:{server.server_address[1]}/v1", "m", None, 5
    )
    server.shutdown()


def error(
    status: int, message: str = "busy", code: str | None = None, **headers
) -> tuple:
    body = {"error": {"message": message, **({"code": code} if code else {})}}
    return (
        status,
        body,
        {name.replace("_", "-"): value for name, value in headers.items()},
    )


# -- retries ----------------------------------------------------------------


def test_a_rate_limit_waits_as_long_as_the_endpoint_asks(client, waits):
    Scripted.script = [error(429, retry_after_ms="250"), ANSWER]

    assert client.complete(HISTORY, [])["content"] == "ok"
    assert waits == [0.25]


def test_server_errors_back_off_with_jitter(client, waits):
    Scripted.script = [error(503), error(502), ANSWER]

    assert client.complete(HISTORY, [])["content"] == "ok"
    assert Scripted.count == 3
    # Full jitter: anywhere up to 1 s, then up to 2 s.
    assert 0 <= waits[0] <= 1 and 0 <= waits[1] <= 2


def test_a_refused_request_is_never_retried(client, waits):
    Scripted.script = [error(400, "bad schema")]

    with pytest.raises(ModelError) as caught:
        client.complete(HISTORY, [])

    assert caught.value.status == 400 and not caught.value.retryable
    assert Scripted.count == 1 and waits == []


def test_retries_stop_at_the_limit(client, waits, monkeypatch):
    monkeypatch.setenv("HARNESS_MODEL_RETRIES", "2")
    Scripted.script = [error(503)] * 3

    with pytest.raises(ModelError) as caught:
        client.complete(HISTORY, [])

    assert caught.value.status == 503
    assert Scripted.count == 3 and len(waits) == 2


def test_a_long_retry_after_is_capped(client, waits):
    Scripted.script = [error(429, retry_after="3600"), ANSWER]

    client.complete(HISTORY, [])

    assert waits == [model.MAX_RETRY_WAIT_SECONDS]


def test_retry_after_may_be_an_http_date():
    class Headers(dict):
        def get(self, name, default=None):
            return super().get(name.lower(), default)

    in_ten = formatdate(time.time() + 10, usegmt=True)

    assert 8 <= _retry_after(Headers({"retry-after": in_ten})) <= 10
    assert _retry_after(Headers({"retry-after": "soon"})) is None


def test_a_slow_endpoint_is_a_transient_error_not_a_crash(client, waits, monkeypatch):
    monkeypatch.setenv("HARNESS_MODEL_RETRIES", "0")
    Scripted.script, Scripted.delay = [ANSWER], 1.5
    client.timeout = 0.3

    with pytest.raises(ModelError) as caught:
        client.complete(HISTORY, [])

    assert caught.value.transient and "did not answer" in str(caught.value)


def test_nothing_listening_is_reported_at_once(waits):
    nobody = ChatCompletionsClient("http://127.0.0.1:9/v1", "m", None, 5)

    with pytest.raises(ModelError) as caught:
        nobody.complete(HISTORY, [])

    assert not caught.value.retryable and waits == []


# -- context overflow -------------------------------------------------------


def test_an_overflow_is_recognised_by_its_code_or_its_words(client, waits):
    Scripted.script = [
        error(400, "Input is longer than allowed.", code="context_length_exceeded"),
        error(400, "This model's maximum context length is 8192 tokens."),
        error(400, "Unknown parameter: tools."),
    ]
    caught = []

    for _ in range(3):
        with pytest.raises(ModelError) as raised:
            client.complete(HISTORY, [])
        caught.append(raised.value)

    assert caught[0].code == "context_length_exceeded"
    assert [item.context_overflow for item in caught] == [True, True, False]


class OverflowingModel:
    url = "scripted"

    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools, response_format=None):
        self.calls += 1
        raise ModelError("too long", 400, code="context_length_exceeded")


class NoTools(ToolLoop):
    async def run_calls(self, calls):
        return []


def test_the_loop_reports_an_overflow_as_one_not_as_a_refusal():
    overflowing = OverflowingModel()
    loop = NoTools(
        HISTORY,
        overflowing,
        [{"type": "function"}],
        answer_format=lambda: {"type": "json_schema"},
        tools_required=True,
    )

    with pytest.raises(ModelError) as caught:
        asyncio.run(loop.reply())

    assert "context window" in str(caught.value)
    assert "HARNESS_MODEL_API" not in str(caught.value)
    # Not retried without the schema or the tools: neither would help.
    assert overflowing.calls == 1 and loop.answer_format is not None
