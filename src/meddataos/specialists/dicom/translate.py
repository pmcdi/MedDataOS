"""Convert between Chat Completions messages and Agents SDK input items.

MedDataOS re-sends the whole conversation on every POST, so each request is turned
into a fresh SDK input. Assistant tool calls become ``function_call`` items and
``role: tool`` messages become ``function_call_output`` items.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

SYSTEM_ROLES = ("system", "developer")


def text_of(content: Any) -> str:
    """Plain text from a string or a list of content parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                text_parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                text_parts.append(part)
        return "\n".join(text_parts)

    return str(content)


def system_prompt(messages: list[dict]) -> str:
    """All system and developer messages, joined."""
    return "\n\n".join(
        text_of(message.get("content"))
        for message in messages
        if message.get("role") in SYSTEM_ROLES
    )


def conversation_key(messages: list[dict]) -> str:
    """A stable id for one consult: the system prompt plus the first user message.

    MedDataOS sends no session id. These two messages are the same on every POST
    of one consult and differ between consults, so they pick the workspace.
    """
    first_user_text = next(
        (
            text_of(message.get("content"))
            for message in messages
            if message.get("role") == "user"
        ),
        "",
    )

    digest = hashlib.sha256()
    digest.update(system_prompt(messages).encode())
    digest.update(b"\0")  # separator, so "ab" + "c" and "a" + "bc" hash differently
    digest.update(first_user_text.encode())
    return digest.hexdigest()[:24]


def to_input_items(messages: list[dict]) -> list[dict[str, Any]]:
    """Chat messages (without system) as Agents SDK input items."""
    items: list[dict[str, Any]] = []

    for message in messages:
        role = message.get("role")

        if role in SYSTEM_ROLES:
            continue  # sent as the agent's instructions instead

        if role == "user":
            items.append({"role": "user", "content": text_of(message.get("content"))})

        elif role == "assistant":
            text = text_of(message.get("content"))
            if text:
                items.append({"role": "assistant", "content": text})

            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id", ""),
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments") or "{}",
                    }
                )

        elif role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id", ""),
                    "output": text_of(message.get("content")),
                }
            )

    return items


def completion(
    model: str, content: str | None, tool_calls: list[dict] | None = None
) -> dict[str, Any]:
    """A Chat Completions response with either text or tool calls."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
    }


def tool_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    """One Chat Completions tool call."""
    # MedDataOS parses the arguments as JSON; send {} rather than a string it
    # would reject.
    try:
        json.loads(arguments or "{}")
    except json.JSONDecodeError:
        arguments = "{}"

    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments or "{}"},
    }
