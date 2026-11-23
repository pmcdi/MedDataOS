"""The harness model clients: the Responses translation and both HTTP clients."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from meddataos.core.model import (
    ChatCompletionsClient,
    ModelError,
    ModelSettings,
    ResponsesClient,
    client_from_settings,
    settings_from_env,
    to_chat_message,
    to_responses_format,
    to_responses_input,
    to_responses_tool,
)

FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch",
        "description": "Read a resource.",
        "parameters": {
            "type": "object",
            "properties": {"resource_id": {"type": "string"}},
        },
    },
}
HISTORY = [
    {"role": "system", "content": "You answer questions."},
    {"role": "user", "content": "What was the MELD?"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "fetch", "arguments": '{"resource_id": "t1"}'},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call_1", "content": '{"cite": "c1"}'},
]


def test_a_chat_history_becomes_instructions_and_input_items():
    instructions, items = to_responses_input(HISTORY)

    assert instructions == "You answer questions."
    assert items == [
        {"role": "user", "content": "What was the MELD?"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "fetch",
            "arguments": '{"resource_id": "t1"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"cite": "c1"}',
        },
    ]


def test_tools_and_the_answer_schema_take_the_responses_shape():
    schema = {"type": "object", "properties": {"response": {"type": "string"}}}
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "cited_answer", "schema": schema, "strict": True},
    }

    assert to_responses_tool(FETCH_TOOL) == {
        "type": "function",
        "name": "fetch",
        "description": "Read a resource.",
        "parameters": FETCH_TOOL["function"]["parameters"],
        "strict": False,
    }
    assert to_responses_format(response_format) == {
        "type": "json_schema",
        "name": "cited_answer",
        "schema": schema,
        "strict": True,
    }
    assert to_responses_format({"type": "text"}) is None


def test_responses_output_becomes_one_chat_message():
    output = [
        {"type": "reasoning", "summary": []},
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_9",
            "name": "fetch",
            "arguments": "{}",
        },
        {"type": "message", "content": [{"type": "output_text", "text": "Reading."}]},
    ]

    assert to_chat_message(output) == {
        "role": "assistant",
        "content": "Reading.",
        "tool_calls": [
            {
                "id": "call_9",
                "type": "function",
                "function": {"name": "fetch", "arguments": "{}"},
            }
        ],
    }


def test_azure_settings_win_when_configured(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "secret")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-test")
    monkeypatch.setenv("AZURE_OPENAI_REASONING_EFFORT", "low")
    monkeypatch.setenv("HARNESS_MODEL_API", "responses")
    settings = settings_from_env()

    assert settings.base_url == "https://example.azure.com/openai/v1"
    assert (settings.model, settings.key_header, settings.reasoning_effort) == (
        "gpt-test",
        "api-key",
        "low",
    )
    assert isinstance(client_from_settings(settings), ResponsesClient)


def test_openai_compatible_settings_otherwise(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1/")
    monkeypatch.setenv("OPENAI_MODEL", "llama")
    monkeypatch.delenv("HARNESS_MODEL_API", raising=False)
    settings = settings_from_env()

    assert (settings.api, settings.base_url, settings.key_header) == (
        "chat",
        "http://127.0.0.1:11434/v1",
        "Authorization",
    )
    assert isinstance(client_from_settings(settings), ChatCompletionsClient)

    with pytest.raises(ValueError, match="expected chat or responses"):
        client_from_settings(ModelSettings(api="grpc", base_url="x", model="m"))


class Recorder(BaseHTTPRequestHandler):
    """Records each request and answers with a canned body for its path."""

    requests: list[tuple[str, dict, dict]] = []
    replies: dict[str, tuple[int, dict]] = {}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # Header names are case-insensitive; urllib sends Api-key, not api-key.
        headers = {name.lower(): value for name, value in self.headers.items()}
        Recorder.requests.append((self.path, headers, body))
        status, payload = Recorder.replies[self.path]
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def endpoint():
    Recorder.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


def test_the_responses_client_sends_a_stateless_request(endpoint):
    Recorder.replies = {
        "/v1/responses": (
            200,
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "17"}],
                    }
                ]
            },
        )
    }
    settings = ModelSettings(
        api="responses",
        base_url=endpoint,
        model="gpt-test",
        key="secret",
        key_header="api-key",
        reasoning_effort="low",
    )

    message = ResponsesClient(settings).complete(HISTORY, [FETCH_TOOL])
    path, headers, body = Recorder.requests[0]

    assert message == {"role": "assistant", "content": "17"}
    assert path == "/v1/responses"
    assert headers["api-key"] == "secret"
    assert body["store"] is False
    assert body["reasoning"] == {"effort": "low"}
    assert body["instructions"] == "You answer questions."
    assert body["tools"][0]["name"] == "fetch"


def test_the_chat_client_sends_reasoning_effort_and_a_bearer_key(endpoint):
    Recorder.replies = {
        "/v1/chat/completions": (
            200,
            {"choices": [{"message": {"role": "assistant", "content": "hi"}}]},
        )
    }
    client = ChatCompletionsClient(
        endpoint, "llama", "secret", 5, reasoning_effort="low"
    )

    assert client.complete([{"role": "user", "content": "hi"}], [])["content"] == "hi"
    _, headers, body = Recorder.requests[0]
    assert headers["authorization"] == "Bearer secret"
    assert body["reasoning_effort"] == "low"
    assert "tools" not in body


def test_an_http_error_carries_its_status_and_message(endpoint):
    Recorder.replies = {"/v1/responses": (400, {"error": {"message": "tools refused"}})}
    settings = ModelSettings(api="responses", base_url=endpoint, model="m")

    with pytest.raises(ModelError) as caught:
        ResponsesClient(settings).complete(HISTORY, [FETCH_TOOL])

    assert caught.value.status == 400
    assert "tools refused" in str(caught.value)


def test_usage_is_read_from_either_api():
    from meddataos.core.model import usage_from

    responses = {
        "usage": {
            "input_tokens": 900,
            "output_tokens": 40,
            "input_tokens_details": {"cached_tokens": 512},
        }
    }
    chat = {"usage": {"prompt_tokens": 700, "completion_tokens": 30}}

    assert usage_from(responses) == {"input": 900, "output": 40, "cached": 512}
    assert usage_from(chat) == {"input": 700, "output": 30, "cached": 0}
    assert usage_from({"choices": []}) is None


def test_a_client_remembers_the_last_responses_usage(endpoint):
    Recorder.replies = {
        "/v1/responses": (
            200,
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
                "usage": {"input_tokens": 120, "output_tokens": 8},
            },
        )
    }
    client = ResponsesClient(
        ModelSettings(api="responses", base_url=endpoint, model="m")
    )

    client.complete(HISTORY, [])

    assert client.last_usage == {"input": 120, "output": 8, "cached": 0}
