"""Hold the conversation with the model or agent for each question.

With --agent-url the router is any OpenAI-compatible chat completions endpoint:
a plain model, or an agent served behind that API. With --harness the server
talks to the model itself, through ``meddataos.harness``. Either way, tool calls
are run here on the open case, the answer is sent back for correction until the
citations pass, and the accepted answer is pushed to every open page. At startup
the same model or agent also fills in the patient card from the files.

"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from uuid import uuid4

from meddataos.core.datactrl import Case as DatactrlCase
from meddataos.core.loop import ToolLoop
from meddataos.core.model import (
    ChatCompletionsClient,
    ModelClient,
    client_from_settings,
    settings_from_env,
)
from meddataos.core.registry import Evidence, citing_rule, evidence_of
from meddataos.core.verifier import Answer, Contract, schema
from meddataos.core.workspace import offload_payloads, workspace_for
from meddataos.harness.agents import (
    AGENTS_DIR,
    Agent,
    load_agents,
    offered_tools,
    spawn_tool,
)
from meddataos.harness.mcp import summary as mcp_summary
from meddataos.harness.outbound import OutboundScreen
from meddataos.harness.tools import HarnessTools
from meddataos.server import findings, store
from meddataos.server.case import Profile, case
from meddataos.server.state import log, short_session, state

_tasks: set[asyncio.Task] = set()


def _background(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


_TOOL_LABELS = {
    "discover": "Discovering the case",
    "describe": "Describing",
    "fetch": "Reading",
    "notes_search": "Searching the notes",
    "links": "Listing links",
    "assert_link": "Checking a link",
    "dicom_tree": "Reading the DICOM tree",
    "dicom_groups": "Grouping DICOM series",
    "reference_search": "Searching references",
    "reference_read": "Reading a reference",
    "load_skill": "Loading a skill",
    "ledger_query": "Searching the notes ledger",
    "notes_timeline": "Reading the notes timeline",
    "notes_conflicts": "Checking where the notes disagree",
}


def _file_label(arguments: dict) -> str:
    """The case file's label for a resource id, without reading the file."""
    resource_id = arguments.get("resource_id")

    if not isinstance(resource_id, str):
        return ""

    for item in case.files():
        if item.get("id") != resource_id:
            continue

        label = item.get("label")

        if isinstance(label, str) and label.strip():
            return label.strip()

        path = item.get("web_path") or ""
        return str(path).rsplit("/", 1)[-1]

    return ""


def _tool_label(name: str, arguments: dict) -> str:
    # mcp__<server>__<tool> reads as "Running <tool> on <server>".
    if name.startswith("mcp__") and name.count("__") >= 2:
        _, server, tool = name.split("__", 2)
        return f"Running {tool} on {server}"

    action = _TOOL_LABELS.get(name, name or "Running a tool")
    target = _file_label(arguments)

    if target and name in ("describe", "fetch"):
        return f"{action} {target}"

    return action


def _cite_kind(modality: str) -> str:
    """The citation type a finished read stands for, from the file's modality."""
    kinds = {
        "dicom": "imaging",
        "note": "note",
        "structured": "table",
        "derived": "derived",
    }
    return kinds.get(modality, "")


def _stamp_answer_kinds(steps: list[dict], message: str) -> None:
    """Use the delivered citation's kind when the answer names one for that token."""
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        return

    citations = parsed.get("citations") if isinstance(parsed, dict) else None

    if not isinstance(citations, list):
        return

    kinds = {}
    sources = {}

    for item in citations:
        if not isinstance(item, dict) or not isinstance(item.get("cite"), str):
            continue

        if isinstance(item.get("kind"), str) and item["kind"]:
            kinds[item["cite"]] = item["kind"]

        if item.get("kind") != "derived" or not isinstance(item.get("inputs"), list):
            continue

        used = []

        for entry in item["inputs"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("cite"), str):
                continue

            label = entry.get("label") if isinstance(entry.get("label"), str) else ""
            used.append(f"{entry['cite']} {label}".strip())

        if used:
            sources[item["cite"]] = used

    for step in steps:
        kind = kinds.get(step.get("cite"))

        if kind:
            step["cite_kind"] = kind

        step_sources = sources.get(step.get("cite"))

        if step_sources:
            step["sources"] = step_sources


_step_started: dict[str, float] = {}


def _live(session_id: str):
    """The session while its question is open.

    The patient card and replies that arrive late have none.
    """
    if not session_id or session_id not in state.pending:
        return None

    return state.sessions.get(session_id)


async def record_step(session_id: str, step: dict) -> None:
    """Remember one step on the session and push it, timed once it ends."""
    session = _live(session_id)

    if session is None:
        return

    step_id = step["step_id"]
    stored = next((item for item in session.steps if item["step_id"] == step_id), None)
    fields = {key: value for key, value in step.items() if value is not None}
    now = time.monotonic()

    if stored is None:
        stored = fields
        stored["started_ms"] = session.elapsed_ms()
        session.steps.append(stored)
        _step_started[step_id] = now
    else:
        stored.update(fields)

    if stored.get("status") in ("done", "error"):
        began = _step_started.pop(step_id, now)
        stored["duration_ms"] = int((now - began) * 1000)

    await state.hub.step(session_id, dict(stored), session.elapsed_ms())


async def set_status(session_id: str, label: str) -> None:
    """Say what the open question is waiting on: the model, or the citation check."""
    session = _live(session_id)

    if session is None or session.status == label:
        return

    session.status = label
    await state.hub.status(session_id, label, session.elapsed_ms())


def router_client() -> ModelClient:
    """The model or agent the router conversation and the patient card talk to.

    With --harness it is the model itself, from the harness settings. Otherwise
    it is the agent at --agent-url.
    """
    if state.harness:
        return client_from_settings(settings_from_env())

    return _agent_client(state.agent_url or "")


# How often one question may consult. A repeat of the same call is answered from
# the first one and does not count.
MAX_CONSULTS_PER_SUBAGENT = 3
MAX_CONSULTS = 8

# What a consult step keeps of the question and the reply, for the trace.
MAX_STEP_TEXT = 700


def _consults(session_id: str) -> dict:
    """The consult book for the open question: calls made, counts, and failures."""
    return state.consults.setdefault(
        session_id, {"tasks": {}, "count": {}, "failed": set()}
    )


def calls_by(session_id: str, by: str) -> dict[str, dict]:
    """The reads one party made for this question, by cite token."""
    return {
        token: call
        for token, call in state.calls.get(session_id, {}).items()
        if call.get("by", "router") == by
    }


class Conversation(ToolLoop):
    """The harness loop with the server's tools: case reads, and consults."""

    def __init__(
        self,
        messages: list[dict],
        session_id: str,
        client: ModelClient | None = None,
        tools: list[dict] | None = None,
        consult: bool = False,
        max_steps: int | None = None,
        answer_format: Callable[[], dict | None] | None = None,
        activity: str = "Thinking",
        by: str = "router",
        harness_tool_names: Iterable[str] | None = None,
        offload: bool | None = None,
        parent_step: str | None = None,
    ) -> None:
        """The messages so far, plus how tool calls and the answer format work.

        ``by`` names who this conversation is with, and so who made its reads.
        ``harness_tool_names`` are the harness tools it may run (by default all of
        them for the harness's router, none otherwise). ``offload`` saves payloads
        to the workspace (by default for the harness's router only).
        """
        super().__init__(
            messages,
            client or router_client(),
            list(case.tools if tools is None else tools),
            max_steps,
            answer_format,
            # The harness's own models must be able to read; an external agent
            # may still answer without tools.
            tools_required=state.harness,
        )
        self.session_id = session_id
        self.consult = consult
        self.activity = activity
        self.by = by
        self.parent_step = parent_step
        harness_router = state.harness and by == "router"

        if harness_tool_names is None:
            harness_tool_names = harness_tools().names if harness_router else ()

        self.harness_tool_names = set(harness_tool_names)
        # Only the harness's own models get payloads as workspace paths. An
        # external agent at --agent-url decodes the base64 it is sent itself.
        self.offload = harness_router if offload is None else offload

    async def _record(self, step: dict) -> None:
        """Record one step, tagged with the subagent that made it.

        The page shows a subagent's own reads under its row, so the trace says
        which party read what.
        """
        if self.by != "router":
            step = {**step, "by": self.by, "parent": self.parent_step}

        await record_step(self.session_id, step)

    async def waiting(self) -> None:
        """Waiting on the model updates the live status. It does not add a row."""
        await set_status(self.session_id, self.activity)

    def prepare_result(self, result: dict) -> object:
        """Volumes, previews and PDF pages go to the workspace, not the context.

        An image to look at is left out here and sent by ``follow_up``.
        """
        if isinstance(result, dict) and "attach_image" in result:
            result = {
                key: value for key, value in result.items() if key != "attach_image"
            }

        if not self.offload:
            return result

        return offload_payloads(result, workspace_for(self.session_id or "card"))

    def follow_up(self, results: list[dict]) -> list[dict]:
        """A user message per image the model asked to see.

        Tool messages hold only text in the chat and Responses APIs, so the image
        comes as the next user turn, named so the model knows where it is from.
        """
        messages = []

        for result in results:
            image = result.get("attach_image") if isinstance(result, dict) else None

            if not isinstance(image, dict):
                continue

            data_url = f"data:{image['mime_type']};base64,{image['image_base64']}"
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{result['name']} {image['path']}:"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            )

        return messages

    def record_usage(self, usage: dict) -> None:
        """Add one response's tokens to this question's run, under who asked."""
        run = state.runs.get(self.session_id)

        if run is None:
            return

        totals = run.setdefault("usage", {}).setdefault(
            self.by, {"calls": 0, "input": 0, "output": 0, "cached": 0}
        )
        totals["calls"] += 1

        for key in ("input", "output", "cached"):
            totals[key] += int(usage.get(key) or 0)

    def notice(self, text: str) -> None:
        """Log what the loop reports."""
        log("server", text)

    async def run_calls(self, calls: list[dict]) -> list[dict]:
        """Run one turn's tool calls; see ``_run_calls``."""
        return await self._run_calls(calls)

    async def _run_calls(self, calls: list[dict]) -> list[dict]:
        """Run one turn's tool calls and return their results in the same order.

        Subagents spawned in one turn run at the same time: each waits on its
        own model. The other tools are quick and run one after another.
        """
        consults = {
            index: asyncio.ensure_future(self._run_call(call))
            for index, call in enumerate(calls)
            if (call.get("function") or {}).get("name") == "spawn_subagent"
        }
        results: list[dict] = []

        for index, call in enumerate(calls):
            if index in consults:
                results.append({})
            else:
                results.append(await self._run_call(call))

        for index, task in consults.items():
            results[index] = await task

        return results

    async def _run_call(self, call: dict) -> dict:
        function = call.get("function") or {}
        name = function.get("name", "")

        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            await self._tool_step(name, {}, {"status": "error"})
            return {
                "status": "error",
                "name": name,
                "message": f"Arguments are not valid JSON: {exc}",
            }

        if not isinstance(arguments, dict):
            await self._tool_step(name, {}, {"status": "error"})
            return {
                "status": "error",
                "name": name,
                "message": "Arguments must be a JSON object",
            }

        if name == "spawn_subagent":
            return await self._spawn(arguments)

        # References and skills read nothing from the case, so they run here
        # without a cite token. Only the harness's own model is offered them.
        if name in self.harness_tool_names:
            return await self._run_harness_tool(name, arguments)

        step_id = uuid4().hex[:12]
        await self._record(
            {
                "step_id": step_id,
                "kind": "tool",
                "status": "started",
                "label": _tool_label(name, arguments),
                "name": name,
            },
        )
        result = await asyncio.to_thread(
            case.run, name, arguments, self.session_id, self.by
        )
        await self._finish_tool(step_id, name, arguments, result)
        return result

    async def _run_harness_tool(self, name: str, arguments: dict) -> dict:
        """Run one of the harness's tools, shown as a step like any other.

        A derived tool's output (an exec run) is recorded under a cite token, so
        a value it printed can be cited and checked.
        """
        step_id = uuid4().hex[:12]
        await self._record(
            {
                "step_id": step_id,
                "kind": "tool",
                "status": "started",
                "label": _tool_label(name, arguments),
                "name": name,
            },
        )
        result = await asyncio.to_thread(
            harness_tools().run,
            name,
            arguments,
            workspace_for(self.session_id or "card"),
            state.root,
        )
        log(
            "agent",
            f"session={short_session(self.session_id)} {name} {result['status']}",
        )

        if result["status"] == "ok" and evidence_of(name) is Evidence.DERIVED:
            token = case.remember_output(
                self.session_id, name, arguments, result["result"], self.by
            )

            if token:
                result = {**result, "cite": token}

        await self._finish_tool(step_id, name, arguments, result)
        return result

    async def _tool_step(self, name: str, arguments: dict, result: dict) -> None:
        """Record a tool call that failed before it ran."""
        step_id = uuid4().hex[:12]
        await self._record(
            {
                "step_id": step_id,
                "kind": "tool",
                "status": "started",
                "label": _tool_label(name, arguments),
                "name": name,
            },
        )
        await self._finish_tool(step_id, name, arguments, result)

    async def _finish_tool(
        self, step_id: str, name: str, arguments: dict, result: dict
    ) -> None:
        cite = result.get("cite") if isinstance(result, dict) else None
        label = _tool_label(name, arguments)

        if isinstance(cite, str):
            record = state.calls.get(self.session_id, {}).get(cite) or {}
            filename = record.get("file")

            if isinstance(filename, str) and filename and name in ("describe", "fetch"):
                verb = "Describing" if name == "describe" else "Reading"
                label = f"{verb} {filename}"

        step = {
            "step_id": step_id,
            "kind": "tool",
            "status": "error"
            if not isinstance(result, dict) or result.get("status") == "error"
            else "done",
            "label": label,
            "name": name,
            "detail": step_detail(name, arguments, result),
        }

        if isinstance(cite, str):
            step["cite"] = cite
            cite_kind = _cite_kind(str(record.get("modality") or ""))

            if cite_kind:
                step["cite_kind"] = cite_kind

        await self._record(step)

    async def _spawn(self, arguments: dict) -> dict:
        """Run an agent in its own context, within the question's consult budget."""
        name = "spawn_subagent"

        if not self.consult:
            return _tool_error(name, "This agent cannot start a subagent.")

        requested = str(arguments.get("agent") or "")
        agent = offered_agents().get(requested)

        if agent is None:
            names = ", ".join(offered_agents()) or "none"
            return _tool_error(name, f"Unknown agent {requested!r}. Agents: {names}")

        async def ask(
            question: str, context: str, resource_ids: list[str], run_step: str
        ) -> dict:
            return await consult_subagent(
                agent,
                question,
                self.session_id,
                context,
                resource_ids,
                parent_step=run_step,
            )

        return await self._book(agent.name, name, "task", arguments, ask)

    async def _book(
        self,
        target: str,
        name: str,
        question_field: str,
        arguments: dict,
        ask: Callable[[str, str, list[str], str], Awaitable[dict]],
    ) -> dict:
        """One subagent run, counted against the question's budget.

        The same call made twice gets the first answer. A different question, new
        context, or other resources is a new consult, up to the budget.
        """
        question = arguments.get(question_field)

        if not isinstance(question, str) or not question.strip():
            return _tool_error(name, f"{question_field} must be a non-empty string")

        raw_context = arguments.get("context")
        context = raw_context.strip() if isinstance(raw_context, str) else ""
        raw_ids = arguments.get("resource_ids")
        resource_ids = [
            item.strip()
            for item in (raw_ids if isinstance(raw_ids, list) else [])
            if isinstance(item, str) and item.strip()
        ]
        book = _consults(self.session_id)
        key = (
            target,
            " ".join(question.lower().split()),
            " ".join(context.lower().split()),
            tuple(resource_ids),
        )
        asked = book["tasks"].get(key)

        if asked is not None:
            log(
                "agent",
                f"session={short_session(self.session_id)} "
                f"consult {target} already asked that",
            )
            earlier: dict = await asked
            return earlier

        used = book["count"].get(target, 0)

        if used >= MAX_CONSULTS_PER_SUBAGENT or len(book["tasks"]) >= MAX_CONSULTS:
            return _tool_error(
                name,
                f"The consult budget for this question is used "
                f"({used} to {target}, {len(book['tasks'])} in all). "
                "Answer from what the subagents returned.",
            )

        book["count"][target] = used + 1
        task = asyncio.ensure_future(
            self._ask(target, question.strip(), context, resource_ids, ask)
        )
        book["tasks"][key] = task
        return await task

    async def _ask(
        self,
        target: str,
        question: str,
        context: str,
        resource_ids: list[str],
        ask: Callable[[str, str, list[str], str], Awaitable[dict]],
    ) -> dict:
        """One subagent run, shown as a step with its task and what came back."""
        step_id = uuid4().hex[:12]
        label = f"Subagent {target}"
        await self._record(
            {
                "step_id": step_id,
                "kind": "subagent",
                "status": "started",
                "label": label,
                "name": target,
                "question": question[:MAX_STEP_TEXT],
                "context": context[:MAX_STEP_TEXT] or None,
            },
        )
        result = await ask(question, context, resource_ids, step_id)
        failed = result.get("status") == "error"

        if failed:
            _consults(self.session_id)["failed"].add(target)

        await self._record(
            {
                "step_id": step_id,
                "kind": "subagent",
                "status": "error" if failed else "done",
                "label": label,
                "name": target,
                "reply": _reply_preview(result),
                "findings": len(result.get("findings") or []) or None,
                "needs": [item["what"] for item in result.get("needs") or []] or None,
                "abstentions": result.get("abstentions") or None,
            },
        )
        return result


MAX_DETAIL = 160


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_DETAIL else text[: MAX_DETAIL - 1] + "…"


def step_detail(name: str, arguments: dict, result: object) -> str | None:
    """One line on what a tool call did, or why it failed, for the trace."""
    if not isinstance(result, dict):
        return None

    if result.get("status") == "error":
        return _clip(f"failed: {result.get('message') or 'no reason given'}")

    raw = result.get("result")
    payload = raw if isinstance(raw, dict) else {}

    if name in ("fetch", "describe"):
        parts = [str(arguments["view"])] if arguments.get("view") else []

        if arguments.get("roi"):
            parts.append(f"roi {arguments['roi']}")

        spacing = arguments.get("spacing")
        if isinstance(spacing, list) and spacing:
            parts.append("×".join(str(value) for value in spacing) + " mm")

        if arguments.get("slice_index") is not None:
            parts.append(f"slice {arguments['slice_index']}")

        for key in ("item", "name", "at"):
            if arguments.get(key):
                parts.append(f"{key} {arguments[key]}")

        return _clip(" · ".join(parts)) if parts else None

    if name == "exec":
        # The command is usually "write a script, then run it": show the run.
        lines = [
            line.strip()
            for line in str(arguments.get("command") or "").splitlines()
            if line.strip()
        ]
        runs = [line for line in lines if line.startswith(("python", "bash", "sh "))]
        ran = runs[-1] if runs else (lines[-1] if lines else "exec")
        printed = [
            line.strip()
            for line in str(payload.get("stdout") or "").splitlines()
            if line.strip()
        ]
        detail = f"{ran} → {printed[-1]}" if printed else ran

        if payload.get("exit_code") not in (0, None):
            detail += f" (exit {payload['exit_code']})"

        return _clip(detail)

    if name == "view_image":
        return _clip(str(arguments.get("path") or ""))

    if name.startswith("mcp__"):
        # The arguments that name files or settings, then the first line back.
        shown = " · ".join(
            f"{key} {value}"
            for key, value in arguments.items()
            if isinstance(value, str | int | float) and not isinstance(value, bool)
        )
        returned = mcp_summary(payload)
        return _clip(f"{shown} → {returned}" if returned else shown) or None

    if name == "load_skill":
        return _clip(str(arguments.get("name") or ""))

    if name in ("notes_search", "reference_search"):
        return _clip(f"“{arguments.get('query') or ''}”")

    if name == "reference_read":
        return _clip(str(payload.get("title") or arguments.get("url") or ""))

    if name == "ledger_query":
        filters = [
            str(arguments[key])
            for key in ("category", "attribute", "text", "note_id")
            if arguments.get(key)
        ]
        found = len(payload.get("facts") or [])
        return _clip(f"{' · '.join(filters) or 'all'} → {found} facts")

    if name == "notes_timeline":
        return _clip(f"{len(payload.get('events') or [])} events")

    if name == "notes_conflicts":
        return _clip(f"{len(payload.get('conflicts') or [])} disagreements")

    return None


def _tool_error(name: str, message: str) -> dict:
    return {"status": "error", "name": name, "message": message}


def _reply_preview(result: dict) -> str:
    """The subagent's words, without its citation objects, for the trace."""
    if result.get("status") == "error":
        return str(result.get("message") or "")[:MAX_STEP_TEXT]

    text = str(result.get("result") or "")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text[:MAX_STEP_TEXT]

    if isinstance(parsed, dict) and isinstance(parsed.get("response"), str):
        return str(parsed["response"])[:MAX_STEP_TEXT]

    return text[:MAX_STEP_TEXT]


_harness_tools: HarnessTools | None = None


def harness_tools() -> HarnessTools:
    """The harness's own tools, built on first use and kept for the process."""
    global _harness_tools

    if _harness_tools is None:
        # Reference URLs are screened for this patient's identifiers.
        _harness_tools = HarnessTools(outbound=OutboundScreen(case.identifiers))

        for warning in _harness_tools.warnings:
            log("server", warning)

    return _harness_tools


_agents: dict[str, Agent] | None = None


def offered_agents() -> dict[str, Agent]:
    """Agents the harness router may spawn, built on first use.

    Each file in ``agents/``, unless ``[agent.<name>]`` sets ``enabled = false``.
    """
    global _agents

    if _agents is None:
        harness = harness_tools()
        warnings: list[str] = []
        _agents = load_agents(
            AGENTS_DIR,
            harness.skills,
            harness.mcp.servers,
            on_warning=warnings.append,
        )
        harness.agents = _agents

        for warning in warnings:
            log("server", warning)

    return _agents


def router_tools() -> list[dict]:
    """Datactrl tools, plus the harness's own tools with --harness.

    An external agent at --agent-url gets the datactrl tools only.
    """
    tools = list(case.tools)

    if state.harness:
        harness = harness_tools()
        tools += harness.definitions()
        agents = offered_agents()

        if agents:
            tools.append(
                spawn_tool(
                    agents,
                    harness.mcp.tools,
                    sandbox=harness.sandbox_problem is None,
                )
            )

    return tools


def _agent_client(url: str) -> ChatCompletionsClient:
    """The external agent at --agent-url, spoken to over chat completions."""
    return ChatCompletionsClient(
        url,
        os.getenv("AGENT_MODEL", ""),
        os.getenv("AGENT_API_KEY"),
        float(os.getenv("AGENT_TIMEOUT", "120")),
    )


# The lines a subagent run adds below the task. A subagent that wants them
# splits the message on these; one that does not reads them as part of the text.
CONTEXT_HEADING = "Context from other findings:"
RESOURCES_HEADING = "Use these resources:"


def subagent_request(question: str, context: str, resource_ids: list[str]) -> str:
    """The user message a subagent gets: the task, then context and files."""
    parts = [question]

    if context:
        parts.append(f"{CONTEXT_HEADING}\n{context}")

    if resource_ids:
        parts.append(f"{RESOURCES_HEADING} {', '.join(resource_ids)}")

    return "\n\n".join(parts)


def subagent_instructions() -> str:
    """Prompt for a subagent: cite case files, and do not start other agents."""
    return "\n".join(
        [
            "You are a subagent answering one question about one patient's medical "
            "files. "
            "Use the tools to read the files when you need them. "
            "Ground your answer in the files. "
            "Do not start another subagent.",
            f"Citation rule: {Contract.specialist_rule()}",
            f"Answer schema, with examples: {schema.prompt_schema()}",
            "Beside response and citations, the JSON may carry: abstentions, a list "
            "of what your files do not say; needs, a list of what you lack to "
            'answer, each {"what": ..., "why": ...}; and facts, the keyed values '
            'behind your citations, each {"key": "lab.afp", "value": ..., '
            '"unit": ..., "date": "YYYY-MM-DD", "cite": the marker it belongs to}. '
            "A reply with needs or abstentions and nothing to cite has an empty "
            "citations list.",
            resources_line(),
        ]
    )


async def consult_subagent(
    agent: Agent,
    question: str,
    session_id: str,
    context: str = "",
    resource_ids: list[str] | None = None,
    parent_step: str | None = None,
) -> dict:
    """Run an agent in a fresh context. Never raises.

    ``parent_step`` is the step id of this run, which the agent's own steps
    carry so the page can group them under it.

    The system prompt is the subagent rules, then the agent's body, then each
    skill it lists. It is offered the case and harness tools plus connected
    MCP tools, filtered by its grant. A granted tool that is not available is
    left out. Its reply is checked and kept as findings, exactly as the
    router's own answer is. Finding ids are ``<agent>.<n>``, such as ``dicom.1``.
    """
    name = f"consult_{agent.name}"
    log(
        "server",
        f"session={short_session(session_id)} subagent {agent.name}: {question}",
    )

    try:
        client = agent.client()
    except ValueError as exc:
        return _tool_error(name, str(exc))

    harness = harness_tools()
    mcp_definitions = harness.mcp.definitions_for(set(agent.tools))
    own = harness.own_definitions()
    tools = offered_tools(agent.tools, case.tools, own, mcp_definitions)
    # MCP tools run in the harness too. Any name left out here goes to the case
    # controller, which does not know it.
    harness_names = {item["function"]["name"] for item in [*own, *mcp_definitions]}
    offered_names = {item["function"]["name"] for item in tools}
    conversation = Conversation(
        [
            {
                "role": "system",
                "content": agent.system_prompt(subagent_instructions(), harness.skills),
            },
            {
                "role": "user",
                "content": subagent_request(question, context, resource_ids or []),
            },
        ],
        session_id,
        client=client,
        tools=tools,
        consult=False,
        max_steps=agent.max_steps,
        activity=f"Waiting for {agent.name}",
        by=agent.name,
        harness_tool_names=offered_names & harness_names,
        offload=True,
        parent_step=parent_step,
    )
    return await run_consult(agent.name, conversation, session_id)


async def run_consult(name: str, conversation: Conversation, session_id: str) -> dict:
    """Send refused replies back until one passes, then keep it as findings.

    Never raises.
    """
    label = short_session(session_id)
    tool_name = f"consult_{name}"
    problem = ""

    try:
        for attempt in range(1, Question.MAX_REVISIONS + 2):
            reply = findings.parse_reply(_strip_fence(await conversation.reply()))
            text = message_text(reply.body)

            if not text.strip():
                problem = "The reply was empty."
                verdict = None
            else:
                verdict = Answer(text, [], case.files(), calls_by(session_id, name))
                problem = verdict.problem or ""

            if verdict is not None and verdict.problem is None:
                log("agent", f"session={label} consult {name} ok")
                return findings.accept(session_id, name, reply, text, verdict)

            log(
                "agent",
                f"session={label} consult {name} reply {attempt} refused: {problem}",
            )
            correction = (
                verdict.correction()
                if verdict is not None
                else f"The server refused that answer. {problem}"
            )
            conversation.messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": correction},
            ]
    except Exception as exc:
        log("agent", f"session={label} consult {name} failed: {exc}")
        return _tool_error(tool_name, str(exc))

    return _tool_error(
        tool_name,
        f"The subagent could not produce valid citations. Last problem: {problem}",
    )


class Question:
    """One user question: ask, then send refused answers back until one passes."""

    MAX_REVISIONS = 6
    PROMPT = (
        "You answer questions about one patient's medical files. "
        "Use the tools to read the files before answering. "
        "Ground your answer in the files. "
        "If you open no files, reply to the user in plain text, not JSON. "
        "Otherwise the whole reply is one JSON object that follows the citation "
        "rule below, "
        "with no sentences before or after it."
    )

    def __init__(self, session_id: str) -> None:
        """The session this question belongs to."""
        self.session_id = session_id
        self.label = short_session(session_id)

    def start(self) -> None:
        """Mark the session busy and ask the agent in the background."""
        session = state.sessions[self.session_id]
        session.steps = []
        session.status = ""
        session.started = time.monotonic()
        state.pending.add(self.session_id)
        question = session.history[-1]["content"]
        state.runs[self.session_id] = {
            "run_id": uuid4().hex,
            "question": question,
            "started_at": datetime.now(UTC).isoformat(),
            "refusals": [],
        }
        log("server", f"session={self.label} question sent to the agent: {question}")
        _background(self.answer())

    async def answer(self) -> None:
        """Ask, then send refused answers back until one passes or retries run out."""
        history = state.sessions[self.session_id].history
        conversation = Conversation(
            [{"role": "system", "content": self._instructions()}]
            + [{"role": turn["role"], "content": turn["content"]} for turn in history],
            self.session_id,
            tools=router_tools(),
            consult=self._has_subagents(),
            answer_format=self._answer_format,
        )
        problem = ""

        try:
            for attempt in range(1, self.MAX_REVISIONS + 2):
                sent = _strip_fence(await conversation.reply())

                if self.session_id not in state.pending:
                    return

                await set_status(self.session_id, "Checking citations")
                # The server's checked citation goes in behind each finding the
                # reply cites, before the reply is judged.
                text = message_text(findings.splice(self.session_id, sent))
                opened = state.reads.get(self.session_id, [])
                verdict = Answer(
                    text,
                    opened,
                    case.files(),
                    calls_by(self.session_id, "router"),
                    findings.known(self.session_id),
                )
                problem = verdict.problem or ""

                if verdict.problem is None:
                    await self._deliver(text)
                    return

                run = state.runs.get(self.session_id)

                if run is not None:
                    run["refusals"].append({"attempt": attempt, "problem": problem})

                log(
                    "agent",
                    f"session={self.label} reply {attempt}/{self.MAX_REVISIONS + 1} "
                    f"refused: {problem}",
                )
                sent = message_text(sent)
                log("agent", f"session={self.label} refused reply was: {sent}")
                conversation.messages += [
                    {"role": "assistant", "content": sent},
                    {"role": "user", "content": verdict.correction()},
                ]
                conversation.activity = "Revising the answer"

            await self._deliver(
                "The agent could not produce an answer with valid citations. "
                f"Last problem: {problem}"
            )

        except Exception as exc:
            log("server", f"session={self.label} agent failed: {exc}")
            await self._deliver(f"The agent failed: {exc}")
        finally:
            state.consults.pop(self.session_id, None)
            state.findings.pop(self.session_id, None)

    @staticmethod
    def _has_subagents() -> bool:
        return state.harness and bool(offered_agents())

    def _answer_format(self) -> dict | None:
        """The answer schema, once a read or a finding means the reply must be cited."""
        cited = calls_by(self.session_id, "router") or state.findings.get(
            self.session_id
        )
        return schema.response_format() if cited else None

    def _instructions(self) -> str:
        lines = [
            self.PROMPT,
            f"Citation rule: {Contract.RULE}",
            f"Answer schema, with examples: {schema.prompt_schema()}",
            resources_line(),
        ]

        if state.harness:
            lines.append(harness_tools().prompt())
            offered = [tool["function"]["name"] for tool in router_tools()]
            lines.append(citing_rule(offered))

        if self._has_subagents():
            lines.append(
                "Launch an agent when it has tools you do not (segmentation "
                "models, code execution for its domain), or when the work means "
                "many reads of one kind. Do the work yourself when it is a quick "
                "lookup your own tools can finish. Launch several agents in the "
                "same turn when a question spans areas; they run in parallel. "
                "Use load_skill for guidance you can follow with your own tools."
            )
            lines.append(
                "spawn_subagent returns findings, each with an id such as "
                "dicom.1. Cite a finding by putting [cite:dicom.1] right after "
                "the words it supports; do not copy its citation. needs lists "
                "what it lacks, and abstentions what its files do not say. Each "
                f"agent can be asked up to {MAX_CONSULTS_PER_SUBAGENT} times."
            )

        return "\n".join(lines)

    async def _deliver(self, message: str) -> None:
        """Record the answer once, stop waiting, and push it to every open page."""
        if self.session_id not in state.pending:
            return

        calls = state.calls.get(self.session_id, {})
        message = case.expand_citations(message, calls)
        message = case.name_sources(message, calls)
        message = case.stamp_basis(message, calls)
        message = findings.keep_discrepancies(self.session_id, message)
        state.pending.discard(self.session_id)
        state.reads.pop(self.session_id, None)
        state.calls.pop(self.session_id, None)
        state.findings.pop(self.session_id, None)
        state.consults.pop(self.session_id, None)

        session = state.sessions[self.session_id]
        steps = [dict(item) for item in session.steps]
        _stamp_answer_kinds(steps, message)
        duration_ms = session.elapsed_ms()
        for item in session.steps:
            _step_started.pop(item["step_id"], None)
        session.steps = []
        session.status = ""
        session.started = None
        usage = (state.runs.get(self.session_id) or {}).get("usage")
        session.add("assistant", message, steps, duration_ms, usage=usage)
        session.processing = False

        run = state.runs.pop(self.session_id, None)

        if state.root is not None and run is not None:
            store.append_audit_run(
                state.root,
                {
                    **run,
                    "session_id": self.session_id,
                    "patient_id": session.patient_id,
                    "finished_at": datetime.now(UTC).isoformat(),
                    "duration_ms": duration_ms,
                    "outcome": _audit_outcome(message),
                    "steps": steps,
                    "calls": store.calls_for_audit(calls),
                    "answer": message,
                    "citations": store.citations_for_audit(message),
                },
            )

        state.save()

        await state.hub.assistant(self.session_id, message, steps, duration_ms)
        log(
            "server",
            f"session={self.label} answer sent to "
            f"{len(state.hub.connections)} page(s): {self._preview(message)}",
        )

    @staticmethod
    def _preview(message: str) -> str:
        try:
            parsed = json.loads(message)
        except json.JSONDecodeError:
            return message

        if isinstance(parsed, dict) and isinstance(parsed.get("response"), str):
            citations = parsed.get("citations")
            count = len(citations) if isinstance(citations, list) else 0
            return f"{count} citation(s): {parsed['response']}"

        return message


class PatientCard:
    """The startup request that asks the agent to fill in the sidebar card."""

    MAX_STEPS = 16
    PROMPT = (
        "You fill in the patient card for one patient's medical files. "
        "Use the tools to read the files. Do not guess: leave a field empty when "
        "the files do not say. "
        "Reply with only one JSON object with these keys: "
        '"name" (string), "age" (string, such as "64"), "sex" (string), '
        '"blood_type" (string, or null), "allergies" (list of strings), '
        '"conditions" (list of strings), and "data_dates" (object mapping each '
        "top-level folder of the case to the latest date in it, as YYYY-MM-DD)."
    )

    async def fill(self) -> None:
        """Ask the agent for the card and store whatever the files support."""
        conversation = Conversation(
            [
                {
                    "role": "system",
                    "content": (f"{self.PROMPT}\n{resources_line()}"),
                },
                {"role": "user", "content": "Fill in the patient card."},
            ],
            "",
            max_steps=self.MAX_STEPS,
        )

        try:
            profile = Profile.from_agent(
                json.loads(_strip_fence(await conversation.reply()))
            )
        except Exception as exc:
            log("server", f"patient card not filled, showing the folder name: {exc}")
            return

        state.profile = profile.as_dict()
        filled = {key: value for key, value in state.profile.items() if value}
        summary = json.dumps(filled) if filled else "nothing found in the files"
        log("server", f"patient card filled: {summary}")

        card = case.patient()

        if card:
            await state.hub.broadcast({"type": "patient", "patient": card.as_dict()})


def _audit_outcome(message: str) -> str:
    if message.startswith("The agent could not") or message.startswith(
        "The agent failed"
    ):
        return "failure"
    return "success"


def ask(session_id: str) -> None:
    """Answer the latest question in this session in the background."""
    Question(session_id).start()


def start_indexes() -> None:
    """In harness mode, connect the MCP servers and start the notes ledger.

    Building the ledger calls the notes model once per new or changed note (the
    ledger is cached), so HARNESS_LEDGER=off skips it.
    """
    if not state.harness or state.root is None:
        return

    start_mcp()

    if os.getenv("HARNESS_LEDGER", "on").strip().lower() == "off":
        log("server", "notes ledger: off (HARNESS_LEDGER=off)")
        return

    if state.datactrl_case is None:
        state.datactrl_case = DatactrlCase.open(state.root)

    ledger = harness_tools().ledger
    ledger.on_done = lambda status: log("server", f"notes ledger: {status}")
    ledger.start(state.datactrl_case, state.root.name)
    log("server", f"notes ledger: {ledger.status} ({ledger.note_count} notes)")


def start_mcp() -> None:
    """Connect the configured MCP servers in the background, then log each status.

    A container server can take a while to start (TotalSegmentator loads torch),
    so the page does not wait: its tools join once it has connected, and
    /api/health shows each server's status meanwhile.
    """
    hub = harness_tools().mcp

    if not hub.servers:
        return

    def connect() -> None:
        hub.start()

        for name, status in hub.status.items():
            log("server", f"mcp {name}: {status}")

    threading.Thread(target=connect, name="mcp-start", daemon=True).start()


def mcp_status() -> dict[str, str]:
    """Each configured MCP server's status, for /api/health."""
    if not state.harness or _harness_tools is None:
        return {}

    return dict(_harness_tools.mcp.status)


def stop_indexes() -> None:
    """Close the MCP servers' connections, at shutdown."""
    if _harness_tools is not None:
        _harness_tools.mcp.stop()


# A case with more DICOM series than this lists them by count in the harness
# prompt: TCGA-DD-A4NF's 50 series cost about 13k tokens on every call.
MAX_LISTED_DICOM = 10


def resources_line() -> str:
    """The system prompt's list of case files.

    In harness mode a long run of DICOM series is summarised, since the model
    gets their ids from dicom_groups or dicom_tree anyway. An external agent at
    --agent-url gets the full list.
    """
    files = case.files()
    dicom = [item for item in files if item.get("modality") == "dicom"]

    if not state.harness or len(dicom) <= MAX_LISTED_DICOM:
        return f"Case resources: {json.dumps(files)}"

    listed = [item for item in files if item.get("modality") != "dicom"]
    return (
        f"Case resources: {json.dumps(listed)}\n"
        f"DICOM: {len(dicom)} series, not listed above. dicom_groups or dicom_tree "
        "gives their resource ids."
    )


def fill_card() -> None:
    """Fill the patient card in the background and push it to the pages."""
    log("server", "asking the agent to fill in the patient card")
    _background(PatientCard().fill())


def _strip_fence(text: str) -> str:
    text = text.strip()

    if text.startswith("```"):
        text = text.split("\n", 1)[-1]

        if text.endswith("```"):
            text = text[: text.rfind("```")].strip()

    return text


def message_text(text: str) -> str:
    """Drop a markdown fence, and a JSON wrapper when the answer has no citations."""
    text = _strip_fence(text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text

    if not isinstance(parsed, dict) or parsed.get("citations"):
        return text

    for key in ("message", "response", "text"):
        value = parsed.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return text
