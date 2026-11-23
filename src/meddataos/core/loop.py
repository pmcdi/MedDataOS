"""The tool loop: ask the model, run the tools it calls, and ask again.

The loop knows nothing about cases or citations. A subclass says how to run a
turn's tool calls (``run_calls``), what to show while the model works
(``waiting``), and how to shape a result before the model sees it
(``prepare_result``). The server's ``dispatch.Conversation`` is that subclass.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from meddataos.core.model import ModelClient, ModelError


class ToolLoop:
    """Messages exchanged with one model for one piece of work."""

    MAX_STEPS = 16

    def __init__(
        self,
        messages: list[dict],
        client: ModelClient,
        tools: list[dict],
        max_steps: int | None = None,
        answer_format: Callable[[], dict | None] | None = None,
        tools_required: bool = False,
    ) -> None:
        """The history so far, the model, and the tools it may call.

        ``answer_format`` is asked before each turn, so the answer schema can start
        to apply once the conversation has read something worth citing.
        ``tools_required`` makes a refused tool list an error instead of a retry
        without tools: a harness model that cannot read the case can only write
        answers it cannot support.
        """
        self.messages = messages
        self.client = client
        self.tools = tools
        self.max_steps = self.MAX_STEPS if max_steps is None else max_steps
        self.answer_format = answer_format
        self.tools_required = tools_required

    async def reply(self) -> str:
        """Call the model until it stops asking for tools, then return its text."""
        for _ in range(self.max_steps):
            await self.waiting()
            message = await self._next()
            usage = getattr(self.client, "last_usage", None)

            if usage:
                self.record_usage(usage)

            self.messages.append(message)
            calls = message.get("tool_calls") or []

            if not calls:
                return message.get("content") or ""

            results = await self.run_calls(calls)

            for call, result in zip(calls, results, strict=True):
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": json.dumps(self.prepare_result(result)),
                    }
                )

            # Some results cannot travel as tool output, such as an image: in the
            # standard APIs a tool message holds text only.
            self.messages.extend(self.follow_up(results))

        raise ModelError(f"no answer after {self.max_steps} steps")

    async def waiting(self) -> None:
        """Called before each request to the model."""

    async def run_calls(self, calls: list[dict]) -> list[dict]:
        """Run one turn's tool calls and return their results in the same order."""
        raise NotImplementedError

    def prepare_result(self, result: dict) -> Any:
        """A tool result as the model will see it."""
        return result

    def follow_up(self, results: list[dict]) -> list[dict]:
        """Messages to add after a turn's tool results, such as an image to see."""
        return []

    def record_usage(self, usage: dict) -> None:
        """Called with each response's token counts, when the model reported them."""

    def notice(self, text: str) -> None:
        """Report something worth a log line, such as a refused schema."""

    async def _next(self) -> dict:
        response_format = self.answer_format() if self.answer_format else None

        try:
            return await asyncio.to_thread(
                self.client.complete, self.messages, self.tools, response_format
            )
        except ModelError as exc:
            # Checked first: an overflow is also a 400, and the fallbacks below
            # would drop the schema, then blame the tool API.
            if exc.context_overflow:
                raise ModelError(
                    "The conversation no longer fits in the model's context window "
                    f"({exc}). Ask a narrower question or start a new chat; a long "
                    "run of DICOM reads is the usual cause.",
                    exc.status,
                    code="context_length_exceeded",
                ) from exc

            if exc.status not in (400, 422):
                raise

            # A model that cannot take the answer schema still gets checked
            # afterwards, so dropping the schema loses no safety.
            if response_format:
                self.notice("agent refused the answer schema, retrying without it")
                self.answer_format = None
                return await self._next()

            if not self.tools:
                raise

            if self.tools_required:
                raise ModelError(
                    f"The model refused the tool list, so it cannot read the case "
                    f"({exc}). If the deployment refuses function tools on chat "
                    "completions, set HARNESS_MODEL_API=responses.",
                    exc.status,
                ) from exc

        self.notice("agent refused the tool list, retrying without it")
        self.tools = []
        return await asyncio.to_thread(self.client.complete, self.messages, self.tools)
