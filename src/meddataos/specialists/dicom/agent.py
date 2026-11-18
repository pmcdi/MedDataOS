"""Build and run the DICOM ``SandboxAgent`` for one POST from MedDataOS.

One POST is one turn of work. The agent may run sandbox and reference tools as
often as it needs. When it calls a MedDataOS tool (one of the ``tools`` in the
request), the run stops and those calls go back to MedDataOS as ``tool_calls``.
MedDataOS runs them and POSTs again with the results.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from agents import (
    Agent,
    FunctionTool,
    ModelSettings,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    ToolsToFinalOutputResult,
    function_tool,
    set_tracing_disabled,
)
from agents.models.interface import Model
from agents.sandbox import SandboxAgent
from agents.tool import FunctionToolResult, Tool
from agents.tool_context import ToolContext

import meddataos.specialists.common  # noqa: F401  (loads .env)
from meddataos.specialists.dicom import sandbox, translate, volumes
from meddataos.specialists.dicom.prompt import PREAMBLE
from meddataos.specialists.dicom.references import References

# Tracing would upload run data, including case content, to OpenAI.
set_tracing_disabled(True)

MAX_TURNS = int(os.getenv("DICOM_MAX_TURNS", "24"))

# What the model sees as the result of a MedDataOS tool call within this run.
# The run stops right after, so this text only ends up in the run's own history.
REMOTE_CALL_PLACEHOLDER = "Sent to MedDataOS. Its result arrives with the next message."


@dataclass
class TurnState:
    """What one run collected: MedDataOS tool calls to hand back."""

    remote_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Reply:
    """The outcome of one POST: text, or tool calls for MedDataOS."""

    content: str | None
    tool_calls: list[dict[str, Any]]


# ----------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------


def model_name() -> str:
    return (
        os.getenv("DICOM_AZURE_DEPLOYMENT")
        or os.getenv("AZURE_OPENAI_DEPLOYMENT")
        or os.getenv("DICOM_OPENAI_MODEL")
        or os.getenv("OPENAI_MODEL")
        or ""
    )


@lru_cache(maxsize=1)
def build_model() -> Model:
    """Azure OpenAI when configured, otherwise the OpenAI API.

    ``DICOM_API=chat`` (default) uses Chat Completions. ``DICOM_API=responses``
    uses the Responses API (Azure's /openai/v1 endpoint).
    """
    from agents import OpenAIChatCompletionsModel, OpenAIResponsesModel
    from openai import AsyncAzureOpenAI, AsyncOpenAI

    api_style = os.getenv("DICOM_API", "chat").strip().lower()
    deployment = model_name()
    if not deployment:
        raise RuntimeError("Set DICOM_AZURE_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT.")

    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    azure_key = os.getenv("AZURE_OPENAI_API_KEY", "")

    client: AsyncOpenAI
    if azure_endpoint and azure_key:
        if api_style == "responses":
            # Azure's v1 endpoint speaks the plain OpenAI API, Responses included.
            client = AsyncOpenAI(
                base_url=f"{azure_endpoint}/openai/v1/", api_key=azure_key
            )
        else:
            client = AsyncAzureOpenAI(
                azure_endpoint=azure_endpoint,
                api_key=azure_key,
                api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            )
    else:
        client = AsyncOpenAI(base_url=os.getenv("OPENAI_BASE_URL") or None)

    if api_style == "responses":
        return OpenAIResponsesModel(model=deployment, openai_client=client)
    return OpenAIChatCompletionsModel(model=deployment, openai_client=client)


def model_settings() -> ModelSettings:
    reasoning_effort = os.getenv("DICOM_REASONING_EFFORT") or os.getenv(
        "AZURE_OPENAI_REASONING_EFFORT"
    )
    if not reasoning_effort:
        return ModelSettings()

    from openai.types.shared import Reasoning

    return ModelSettings(reasoning=Reasoning(effort=reasoning_effort))  # type: ignore[typeddict-item]


# ----------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------


def remote_tool(spec: dict[str, Any]) -> FunctionTool:
    """A MedDataOS tool. Calling it records the call; MedDataOS runs it later."""
    function = spec.get("function") or spec
    tool_name = function["name"]

    async def record_call(tool_context: ToolContext[TurnState], arguments: str) -> str:
        tool_context.context.remote_calls.append(
            translate.tool_call(tool_context.tool_call_id, tool_name, arguments)
        )
        return REMOTE_CALL_PLACEHOLDER

    return FunctionTool(
        name=tool_name,
        description=function.get("description", ""),
        params_json_schema=function.get("parameters")
        or {"type": "object", "properties": {}},
        on_invoke_tool=record_call,
        # MedDataOS writes its schemas with optional fields, which strict mode
        # rejects; the server validates the arguments itself.
        strict_json_schema=False,
    )


def stop_on_remote(
    run_context: RunContextWrapper[TurnState], results: list[FunctionToolResult]
) -> ToolsToFinalOutputResult:
    """End the run as soon as the model called a MedDataOS tool.

    Local tools (sandbox, references) let the run continue. A MedDataOS call has
    no result yet, so the run has to stop and hand the call back.
    """
    if run_context.context.remote_calls:
        return ToolsToFinalOutputResult(is_final_output=True, final_output="")
    return ToolsToFinalOutputResult(is_final_output=False)


@lru_cache(maxsize=1)
def references() -> References:
    # One shared instance keeps the site index in memory between requests.
    return References()


def reference_tools() -> list[FunctionTool]:
    reference_library = references()
    site_list = (
        ", ".join(f"{site.name} ({site.base_url})" for site in reference_library.sites)
        or "none"
    )

    @function_tool(
        name_override="reference_search",
        description_override=(
            "Search allowlisted radiology reference sites for background on anatomy, "
            "classification and staging systems, imaging findings and differentials. "
            f"Sites: {site_list}. Use general medical terms, never patient details. "
            "Returns page titles and URLs; read one with reference_read."
        ),
    )
    async def reference_search(
        query: str, site: str | None = None, limit: int = 5
    ) -> str:
        # The search and the page fetches block, so they run off the event loop.
        result = await asyncio.to_thread(reference_library.search, query, site, limit)
        return json.dumps(result, ensure_ascii=False)

    @function_tool(
        name_override="reference_read",
        description_override=(
            "Read the main text of one page from an allowlisted reference site. "
            "section starts the text at the first heading containing that phrase. "
            "Returns the title, headings and up to max_chars of text."
        ),
    )
    async def reference_read(
        url: str, section: str | None = None, max_chars: int = 8000
    ) -> str:
        result = await asyncio.to_thread(
            reference_library.read, url, section, max_chars
        )
        return json.dumps(result, ensure_ascii=False)

    return [reference_search, reference_read]


# ----------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------


class LogTools(RunHooks[TurnState]):
    """Print each local tool call, so sandbox and reference work shows in the log."""

    def __init__(self, consult_label: str) -> None:
        self.consult_label = consult_label

    async def on_tool_start(
        self, context: RunContextWrapper[TurnState], agent: Agent[TurnState], tool: Tool
    ) -> None:
        arguments = getattr(context, "tool_arguments", "") or ""
        print(
            f"dicom  consult={self.consult_label} tool {tool.name} {arguments[:300]}",
            flush=True,
        )


def build_agent(system: str, tools: list[dict[str, Any]]) -> Agent[TurnState]:
    local_tools = reference_tools()
    local_tool_names = {tool.name for tool in local_tools}

    # Skip a MedDataOS tool whose name clashes with a local one; the SDK
    # requires unique tool names.
    remote_tools = [
        remote_tool(spec)
        for spec in tools
        if (spec.get("function") or spec).get("name") not in local_tool_names
    ]

    return SandboxAgent[TurnState](
        name="DICOM specialist",
        instructions=PREAMBLE + "\n" + system,
        model=build_model(),
        model_settings=model_settings(),
        tools=[*remote_tools, *local_tools],
        tool_use_behavior=stop_on_remote,
        capabilities=sandbox.capabilities(),
    )


async def respond(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Reply:
    """Run the agent on one MedDataOS request."""
    consult_key = translate.conversation_key(messages)
    workspace = sandbox.workspace_for(consult_key)
    messages = volumes.externalize(messages, workspace)

    agent = build_agent(translate.system_prompt(messages), tools)
    turn_state = TurnState()
    result = await Runner.run(
        agent,
        translate.to_input_items(messages),  # type: ignore[arg-type]
        context=turn_state,
        max_turns=MAX_TURNS,
        hooks=LogTools(consult_key[:8]),
        run_config=RunConfig(
            sandbox=sandbox.run_config(consult_key), tracing_disabled=True
        ),
    )

    if turn_state.remote_calls:
        return Reply(content=None, tool_calls=turn_state.remote_calls)

    final_output = result.final_output
    answer = final_output if isinstance(final_output, str) else str(final_output or "")
    return Reply(content=answer, tool_calls=[])
