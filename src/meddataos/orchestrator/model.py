"""The model behind the orchestrator: an OpenAI-compatible chat endpoint.

Azure OpenAI is used when AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, and
AZURE_OPENAI_DEPLOYMENT are set. Otherwise OPENAI_MODEL at OPENAI_BASE_URL
(default https://api.openai.com/v1), with OPENAI_API_KEY when set. These are
the settings the example agent reads.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote

# Tries for a name lookup that fails before the model is reached.
DNS_ATTEMPTS = 3
TIMEOUT = float(os.getenv("ORCHESTRATOR_MODEL_TIMEOUT", "110"))


class ModelError(Exception):
    """The model could not be reached or did not return a chat completion."""

    def __init__(self, message: str, status: int = 502) -> None:
        """Keep the HTTP status to pass on."""
        super().__init__(message)
        self.status = status


def configured() -> bool:
    """Whether a model is set up in the environment."""
    azure = (
        os.getenv("AZURE_OPENAI_API_KEY")
        and os.getenv("AZURE_OPENAI_ENDPOINT")
        and os.getenv("AZURE_OPENAI_DEPLOYMENT")
    )
    return bool(azure or os.getenv("OPENAI_MODEL"))


def _target() -> tuple[str, dict, str | None, str | None]:
    """The chat URL, headers, model name (None for Azure), and reasoning effort."""
    key = os.getenv("AZURE_OPENAI_API_KEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")

    if key and endpoint and deployment:
        version = quote(os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"), safe="")
        url = (
            endpoint.rstrip("/")
            + f"/openai/deployments/{quote(deployment, safe='')}/chat/completions"
            + f"?api-version={version}"
        )
        headers = {"Content-Type": "application/json", "api-key": key}
        return url, headers, None, os.getenv("AZURE_OPENAI_REASONING_EFFORT")

    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    headers = {"Content-Type": "application/json"}

    if os.getenv("OPENAI_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['OPENAI_API_KEY']}"

    return (
        base + "/chat/completions",
        headers,
        os.environ["OPENAI_MODEL"],
        os.getenv("OPENAI_REASONING_EFFORT"),
    )


def forward(body: dict) -> tuple[int, dict]:
    """Send one chat completion as it came and return the model's status and body."""
    url, headers, model, effort = _target()
    outgoing: dict[str, Any] = {"messages": body.get("messages") or []}

    if model:
        outgoing["model"] = model

    for key in ("tools", "response_format"):
        if body.get(key):
            outgoing[key] = body[key]

    # Some reasoning models refuse function tools unless this is "none".
    if effort:
        outgoing["reasoning_effort"] = effort

    request = urllib.request.Request(
        url, data=json.dumps(outgoing).encode(), headers=headers
    )

    for attempt in range(DNS_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")

            try:
                payload = json.loads(raw) if raw else {"error": {"message": exc.reason}}
            except json.JSONDecodeError:
                payload = {"error": {"message": raw or str(exc.reason)}}

            return exc.code, payload
        except urllib.error.URLError as exc:
            # A failed name lookup never reached the model, so retrying is safe.
            if isinstance(exc.reason, socket.gaierror) and attempt < DNS_ATTEMPTS - 1:
                time.sleep(2)
                continue

            return 502, {"error": {"message": f"Could not reach the model: {exc.reason}"}}
        except (TimeoutError, json.JSONDecodeError) as exc:
            return 502, {"error": {"message": f"The model did not answer: {exc}"}}

    raise AssertionError("unreachable")


class Model:
    """What the orchestrator asks of a model. Tests pass a scripted one."""

    def forward(self, body: dict) -> dict:
        """The model's own message for a request passed through unchanged."""
        status, payload = forward(body)
        return _message(status, payload)

    def text(self, prompt: str, system: str = "") -> str:
        """A plain-text completion."""
        return str(self._ask(prompt, system, json_mode=False).get("content") or "").strip()

    def json(self, prompt: str, system: str = "") -> Any:
        """A JSON completion, parsed. Falls back to plain mode when it is refused."""
        message = self._ask(prompt, system, json_mode=True)
        return parse_json(str(message.get("content") or ""))

    def _ask(self, prompt: str, system: str, json_mode: bool) -> dict:
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {"messages": messages}

        if json_mode:
            body["response_format"] = {"type": "json_object"}

        status, payload = forward(body)

        if status in (400, 422) and json_mode:
            body.pop("response_format")
            status, payload = forward(body)

        return _message(status, payload)


def _message(status: int, payload: dict) -> dict:
    if status != 200:
        error = payload.get("error") if isinstance(payload, dict) else None
        detail = error.get("message") if isinstance(error, dict) else error
        raise ModelError(f"The model answered {status}: {detail or payload}", status)

    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelError("The model did not return a chat completion") from exc

    if not isinstance(message, dict):
        raise ModelError("The model did not return a chat completion")

    return message


def parse_json(text: str) -> Any:
    """JSON from model text, allowing a code fence or words around it."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start, end = cleaned.find("{"), cleaned.rfind("}")

    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise ModelError(f"The model did not return JSON: {text[:200]!r}")
