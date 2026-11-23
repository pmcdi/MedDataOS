"""The model behind the loop: chat completions, or the Responses API.

Both clients take the loop's chat-shaped history (system, user, assistant turns
with ``tool_calls``, and ``tool`` results) and return one assistant message in the
chat shape. The loop never learns which API answered.

The Responses client exists because some deployments refuse function tools on
chat completions: on 2026-11-18, Azure's gpt-6.1-sol took tools only on
``/openai/v1/responses``.

Settings come from the environment (see ``settings_from_env``):

    HARNESS_MODEL_API      chat (default) or responses
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT,
    AZURE_OPENAI_REASONING_EFFORT
                           Azure's v1 endpoint, used when endpoint and key are set
    OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL, OPENAI_REASONING_EFFORT
                           any other OpenAI-compatible server (Ollama, vLLM, ...)
    HARNESS_MODEL_TIMEOUT  seconds per request (default 120)
    HARNESS_MODEL_RETRIES  retries of a request that failed for a passing reason
                           (default 4); see ``_post``
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

OPENAI_DEFAULT_URL = "https://api.openai.com/v1"
DEFAULT_RETRIES = 4
# Statuses that say "not now", not "not this request".
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
BACKOFF_BASE_SECONDS = 1.0
MAX_RETRY_WAIT_SECONDS = 60.0
# Servers that send no error code still say so in words (vLLM, Ollama, ...).
CONTEXT_OVERFLOW_TEXT = re.compile(
    r"context[ _-]?(length|window)|maximum context|too many tokens|"
    r"prompt is too long|input is too long",
    re.IGNORECASE,
)

log = logging.getLogger(__name__)
# Replaced in tests, so retries do not wait.
sleep = time.sleep


class ModelError(Exception):
    """The model endpoint failed, or returned something that is not an answer."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        code: str | None = None,
        retry_after: float | None = None,
        transient: bool = False,
    ) -> None:
        """What failed, as far as the endpoint said.

        ``code`` is the API's error code (``context_length_exceeded``), and
        ``retry_after`` the seconds the endpoint asked us to wait. ``transient``
        marks a failure with no status that may pass: a dropped connection or a
        timeout.
        """
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.transient = transient

    @property
    def retryable(self) -> bool:
        """Whether the same request may succeed if sent again."""
        return self.transient or self.status in RETRY_STATUSES

    @property
    def context_overflow(self) -> bool:
        """Whether the request was refused for being longer than the model takes."""
        if self.code == "context_length_exceeded":
            return True

        return self.status in (400, 413, 422) and bool(
            CONTEXT_OVERFLOW_TEXT.search(str(self))
        )


class ModelClient(Protocol):
    """What the loop needs from a model: one turn of the conversation."""

    url: str
    # Token counts of the last response, when the endpoint reported them.
    last_usage: dict | None

    def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        response_format: dict | None = None,
    ) -> dict:
        """The next assistant message, in the chat completions shape."""
        ...


@dataclass
class ModelSettings:
    """Where the model is and how to ask it."""

    api: str
    base_url: str
    model: str
    key: str | None = None
    # Azure takes its key in an api-key header; everyone else in a bearer token.
    key_header: str = "Authorization"
    reasoning_effort: str | None = None
    timeout: float = 120.0


def settings_from_env() -> ModelSettings:
    """Model settings from the environment: Azure when it is configured."""
    api = os.getenv("HARNESS_MODEL_API", "chat").strip().lower()
    timeout = float(os.getenv("HARNESS_MODEL_TIMEOUT", "120"))
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    azure_key = os.getenv("AZURE_OPENAI_API_KEY")

    if azure_endpoint and azure_key:
        return ModelSettings(
            api=api,
            # Azure's v1 endpoint speaks the plain OpenAI API, Responses included.
            base_url=f"{azure_endpoint}/openai/v1",
            model=os.getenv("AZURE_OPENAI_DEPLOYMENT", ""),
            key=azure_key,
            key_header="api-key",
            reasoning_effort=os.getenv("AZURE_OPENAI_REASONING_EFFORT") or None,
            timeout=timeout,
        )

    return ModelSettings(
        api=api,
        base_url=(os.getenv("OPENAI_BASE_URL") or OPENAI_DEFAULT_URL).rstrip("/"),
        model=os.getenv("OPENAI_MODEL", ""),
        key=os.getenv("OPENAI_API_KEY") or None,
        reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT") or None,
        timeout=timeout,
    )


def client_from_settings(settings: ModelSettings) -> ModelClient:
    """The client for these settings."""
    if settings.api == "responses":
        return ResponsesClient(settings)

    if settings.api == "chat":
        return ChatCompletionsClient(
            settings.base_url,
            settings.model,
            settings.key,
            settings.timeout,
            key_header=settings.key_header,
            reasoning_effort=settings.reasoning_effort,
        )

    raise ValueError(
        f"Unknown HARNESS_MODEL_API={settings.api!r}; expected chat or responses"
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def usage_from(payload: Any) -> dict | None:
    """Input, output and cached-input tokens from either API's ``usage`` block."""
    usage = payload.get("usage") if isinstance(payload, dict) else None

    if not isinstance(usage, dict):
        return None

    # Chat completions says prompt/completion; the Responses API input/output.
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else 0
    return {
        "input": int(usage.get("input_tokens", usage.get("prompt_tokens")) or 0),
        "output": int(usage.get("output_tokens", usage.get("completion_tokens")) or 0),
        "cached": int(cached or 0),
    }


def _headers(key: str | None, key_header: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}

    if key:
        headers[key_header] = f"Bearer {key}" if key_header == "Authorization" else key

    return headers


def _post(url: str, body: dict, headers: dict[str, str], timeout: float) -> Any:
    """POST with retries for failures that pass: rate limits, 5xx, dropped links.

    A refusal of the request itself (400, 401, 404, 422) is raised at once:
    sending it again gets the same answer. Waits follow the endpoint's
    Retry-After when it sends one, else jittered exponential backoff.
    """
    retries = int(os.getenv("HARNESS_MODEL_RETRIES", str(DEFAULT_RETRIES)))

    for attempt in range(retries + 1):
        try:
            return _post_once(url, body, headers, timeout)
        except ModelError as exc:
            if not exc.retryable or attempt == retries:
                raise

            wait = exc.retry_after
            if wait is None:
                # Full jitter: parallel subagents hitting one limit spread out.
                wait = random.uniform(0, BACKOFF_BASE_SECONDS * 2**attempt)

            wait = min(wait, MAX_RETRY_WAIT_SECONDS)
            log.warning("%s; retry %d/%d in %.1f s", exc, attempt + 1, retries, wait)
            sleep(wait)

    raise AssertionError("unreachable: the last attempt raises")


def _post_once(url: str, body: dict, headers: dict[str, str], timeout: float) -> Any:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        message, code = _http_error(url, exc)
        raise ModelError(message, exc.code, code, _retry_after(exc.headers)) from exc
    except urllib.error.URLError as exc:
        # A dropped or timed-out connection may pass; a refused one, or a name
        # that does not resolve, is a setting to fix and is reported at once.
        passing = isinstance(exc.reason, (TimeoutError, ConnectionResetError))
        raise ModelError(
            f"could not reach {url}: {exc.reason}", transient=passing
        ) from exc
    except (TimeoutError, ConnectionResetError, ConnectionAbortedError) as exc:
        # Raised outside URLError once the request is under way (a read timeout,
        # a server that closed the connection), and would escape the loop.
        raise ModelError(
            f"{url} did not answer: {exc or 'timed out'}", transient=True
        ) from exc
    except json.JSONDecodeError as exc:
        raise ModelError(f"{url} did not return JSON") from exc


def _http_error(url: str, exc: urllib.error.HTTPError) -> tuple[str, str | None]:
    """The error's message for people, and the API's error code if it gave one."""
    detail = ""
    code = None

    try:
        body = json.loads(exc.read() or b"{}")
        error = body.get("error") if isinstance(body, dict) else None

        if isinstance(error, dict):
            detail = str(error.get("message") or "")
            code = str(error.get("code") or "") or None
        else:
            detail = str(error or "")
    except (OSError, json.JSONDecodeError):
        pass

    return f"{url} answered {exc.code}" + (f": {detail}" if detail else ""), code


def _retry_after(headers: Any) -> float | None:
    """Seconds the endpoint asked us to wait: retry-after-ms, or Retry-After."""
    if headers is None:
        return None

    milliseconds = headers.get("retry-after-ms")
    if milliseconds:
        try:
            return max(0.0, float(milliseconds) / 1000)
        except ValueError:
            pass

    value = headers.get("retry-after")
    if not value:
        return None

    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    # The other form is an HTTP date.
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Chat completions
# ---------------------------------------------------------------------------


class ChatCompletionsClient:
    """An OpenAI-compatible chat completions endpoint: a model, or an agent."""

    def __init__(
        self,
        url: str,
        model: str,
        key: str | None,
        timeout: float,
        key_header: str = "Authorization",
        reasoning_effort: str | None = None,
    ) -> None:
        """The base URL (before /chat/completions), model name, key and timeout."""
        self.url = url
        self.model = model
        self.key = key
        self.timeout = timeout
        self.key_header = key_header
        self.reasoning_effort = reasoning_effort
        self.last_usage: dict | None = None

    def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        response_format: dict | None = None,
    ) -> dict:
        """One chat completions request. Returns the assistant message."""
        body: dict = {"model": self.model, "messages": messages}

        if tools:
            body["tools"] = tools

        if response_format:
            body["response_format"] = response_format

        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort

        self.last_usage = None
        payload = _post(
            f"{self.url}/chat/completions",
            body,
            _headers(self.key, self.key_header),
            self.timeout,
        )
        self.last_usage = usage_from(payload)

        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"{self.url} did not return a chat completion") from exc

        if not isinstance(message, dict):
            raise ModelError(f"{self.url} did not return a chat completion")

        return message


# ---------------------------------------------------------------------------
# Responses API
# ---------------------------------------------------------------------------


class ResponsesClient:
    """The Responses API, spoken to with the loop's chat-shaped history.

    Each request sends the whole history and stores nothing on the server, the
    same as chat completions: the loop's messages stay the one record.
    """

    def __init__(self, settings: ModelSettings) -> None:
        """Endpoint, model and key from the settings."""
        self.url = settings.base_url
        self.settings = settings
        self.last_usage: dict | None = None

    def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        response_format: dict | None = None,
    ) -> dict:
        """One Responses request. Returns the assistant message in the chat shape."""
        instructions, items = to_responses_input(messages)
        body: dict = {
            "model": self.settings.model,
            "input": items,
            # Nothing is kept server-side; the next request resends the history.
            "store": False,
        }

        if instructions:
            body["instructions"] = instructions

        if tools:
            body["tools"] = [to_responses_tool(tool) for tool in tools]

        if response_format:
            text_format = to_responses_format(response_format)

            if text_format:
                body["text"] = {"format": text_format}

        if self.settings.reasoning_effort:
            body["reasoning"] = {"effort": self.settings.reasoning_effort}

        self.last_usage = None
        payload = _post(
            f"{self.url}/responses",
            body,
            _headers(self.settings.key, self.settings.key_header),
            self.settings.timeout,
        )
        self.last_usage = usage_from(payload)

        if not isinstance(payload, dict) or not isinstance(payload.get("output"), list):
            raise ModelError(f"{self.url} did not return a response")

        return to_chat_message(payload["output"])


def to_responses_input(messages: list[dict]) -> tuple[str, list[dict]]:
    """System text as instructions, and the other turns as Responses input items."""
    instructions: list[str] = []
    items: list[dict] = []

    for message in messages:
        role = message.get("role")
        content = message.get("content")
        text = content if isinstance(content, str) else ""

        if role == "system":
            instructions.append(text)
            continue

        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": text,
                }
            )
            continue

        # Text in a user or assistant turn; a list is content parts, such as an
        # image the loop attached after a tool result.
        if isinstance(content, list):
            items.append({"role": role, "content": to_responses_parts(content)})
        elif text:
            items.append({"role": role, "content": text})

        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            items.append(
                {
                    "type": "function_call",
                    "call_id": str(call.get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "arguments": str(function.get("arguments") or "{}"),
                }
            )

    return "\n\n".join(part for part in instructions if part), items


def to_responses_parts(parts: list) -> list[dict]:
    """Chat content parts (text, image_url) as Responses input parts."""
    converted: list[dict] = []

    for part in parts:
        if not isinstance(part, dict):
            continue

        if part.get("type") == "text":
            converted.append(
                {"type": "input_text", "text": str(part.get("text") or "")}
            )
        elif part.get("type") == "image_url":
            image = part.get("image_url") or {}
            url = image.get("url") if isinstance(image, dict) else image
            converted.append({"type": "input_image", "image_url": str(url or "")})
        else:
            converted.append(part)

    return converted


def to_responses_tool(tool: dict) -> dict:
    """A chat completions function tool as a Responses function tool."""
    function = tool.get("function") or tool
    return {
        "type": "function",
        "name": function.get("name", ""),
        "description": function.get("description", ""),
        "parameters": function.get("parameters")
        or {"type": "object", "properties": {}},
        # The datactrl schemas have optional fields, which strict mode refuses.
        "strict": False,
    }


def to_responses_format(response_format: dict) -> dict | None:
    """A chat completions response_format as a Responses text format."""
    if response_format.get("type") == "json_schema":
        schema = response_format.get("json_schema") or {}
        return {
            "type": "json_schema",
            "name": schema.get("name", "answer"),
            "schema": schema.get("schema") or {},
            "strict": bool(schema.get("strict")),
        }

    if response_format.get("type") == "json_object":
        return {"type": "json_object"}

    return None


def to_chat_message(output: list[Any]) -> dict:
    """Responses output items as one chat completions assistant message.

    Reasoning items are dropped: with nothing stored, they cannot be referred to
    later, and a stateless tool round trip works without them.
    """
    texts: list[str] = []
    tool_calls: list[dict] = []

    for item in output:
        if not isinstance(item, dict):
            continue

        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    texts.append(str(part.get("text") or ""))

        elif item.get("type") == "function_call":
            tool_calls.append(
                {
                    "id": str(item.get("call_id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or "{}"),
                    },
                }
            )

    message: dict = {"role": "assistant", "content": "".join(texts) or None}

    if tool_calls:
        message["tool_calls"] = tool_calls

    return message
