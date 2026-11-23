"""Tools the harness serves itself, beside the case tools.

References, skills, the sandbox, the notes ledger, and the tools of the MCP
servers registered with ``pixi run mcp``. Results take the case tools' shape,
``{"status", "name", "result"}``, so the loop and the page treat them alike.
None of them reads the case: the server records a derived tool's output (exec,
a derived MCP server) under a cite token itself.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property
from pathlib import Path
from typing import Any

from meddataos.core.datactrl.tools import TOOL_DEFINITIONS
from meddataos.harness.agents import Agent, load_skill_note
from meddataos.harness.ledger import LedgerNotReady, LedgerService
from meddataos.harness.ledger import tool_definitions as ledger_tools
from meddataos.harness.mcp import McpHub
from meddataos.harness.mcp_config import ROUTER, McpConfigError, load_servers
from meddataos.harness.outbound import OutboundScreen
from meddataos.harness.references import References
from meddataos.harness.sandbox import (
    SandboxSettings,
    image_for_model,
    run_command,
    unavailable_reason,
)
from meddataos.harness.skills import SKILLS_DIR, Skill, catalog, load_skills

REFERENCE_GUIDANCE = (
    "reference_search and reference_read give background from allowlisted "
    "radiology sites: anatomy, classification and staging systems, findings. "
    "Search with general medical terms, never patient details. Reference material "
    "is not evidence about this patient: name the page in prose, without a "
    "[cite:] marker."
)


def _definition(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _registered_servers() -> list:
    """The registered MCP servers; a bad registration file stops the server."""
    try:
        return load_servers()
    except McpConfigError as exc:
        raise SystemExit(f"MCP registration: {exc}") from exc


class HarnessTools:
    """The harness's own tools, built once per process."""

    def __init__(
        self,
        skills_dir: Path = SKILLS_DIR,
        references: References | None = None,
        sandbox: SandboxSettings | None = None,
        ledger: LedgerService | None = None,
        mcp: McpHub | None = None,
        outbound: OutboundScreen | None = None,
    ) -> None:
        """Skills, references, sandbox, ledger and MCP servers.

        Skills come from ``skills_dir``, references from configs/references.toml,
        the sandbox from the HARNESS_SANDBOX_* variables, and MCP servers from
        their registration files (``pixi run mcp list``). ``outbound`` screens URLs
        before they leave the machine; the server gives it the case's
        identifiers.
        """
        self.warnings: list[str] = []
        self.skills: dict[str, Skill] = load_skills(
            skills_dir, on_warning=self.warnings.append
        )
        # Filled when agents are loaded, so load_skill can name one.
        self.agents: dict[str, Agent] = {}
        self._references = references
        self.sandbox = sandbox or SandboxSettings.from_env()
        # Started by the server when the case opens; until then it offers nothing.
        self.ledger = ledger or LedgerService()
        # Connected by the server at start; until then its servers offer nothing.
        self.mcp = mcp if mcp is not None else McpHub(_registered_servers())
        self.outbound = outbound or OutboundScreen()

    @cached_property
    def references(self) -> References:
        """The reference library, made on first use: it holds an HTTP client."""
        return self._references or References()

    @cached_property
    def sandbox_problem(self) -> str | None:
        """Why exec and view_image are not offered, or None when they are.

        Checked once: asking Docker for its runtimes on every turn is slow.
        """
        return unavailable_reason(self.sandbox)

    @property
    def names(self) -> set[str]:
        """Names of the tools this object offers the router."""
        return {item["function"]["name"] for item in self.definitions()}

    def definitions(self) -> list[dict]:
        """The function tools to offer the main model.

        MCP tools are included only for servers marked ``router``. An agent
        grants its own MCP tools; see ``agents``.
        """
        return self.own_definitions() + self.mcp.definitions_for(ROUTER)

    def own_definitions(self) -> list[dict]:
        """The harness's own tools, without MCP servers."""
        sites = ", ".join(site.name for site in self.references.sites) or "none"
        tools = [
            _definition(
                "reference_search",
                "Search allowlisted radiology reference sites for background "
                f"(sites: {sites}). Returns page titles and URLs. Use general "
                "medical terms, never patient details.",
                {
                    "query": {"type": "string"},
                    "site": {
                        "type": "string",
                        "description": "One site name, optional",
                    },
                    "limit": {"type": "integer", "description": "1 to 20, default 5"},
                },
                ["query"],
            ),
            _definition(
                "reference_read",
                "Read the main text of one page from an allowlisted reference site. "
                "section starts at the first heading containing that phrase.",
                {
                    "url": {"type": "string"},
                    "section": {"type": "string"},
                    "max_chars": {"type": "integer", "description": "Default 8000"},
                },
                ["url"],
            ),
        ]

        if self.sandbox_problem is None:
            tools += [
                _definition(
                    "exec",
                    "Run a shell command in the sandbox: no network, the case "
                    "read-only at /case, the workspace writable at /workspace "
                    "(volumes/, previews/, documents/ hold what fetch saved; write "
                    "scripts under scripts/ and results under out/). python has "
                    "numpy, scipy, SimpleITK, nibabel, pydicom, scikit-image, "
                    "pandas and matplotlib. Returns exit_code, stdout, stderr.",
                    {"command": {"type": "string"}},
                    ["command"],
                ),
                _definition(
                    "view_image",
                    "Look at a PNG or JPEG in the workspace, such as a preview "
                    "fetch saved or a figure you wrote under out/.",
                    {"path": {"type": "string", "description": "Workspace path"}},
                    ["path"],
                ),
            ]

        if self.ledger.offered:
            tools += ledger_tools()

        if self.skills:
            tools.append(
                _definition(
                    "load_skill",
                    "Load a skill's instructions by name: "
                    + ", ".join(self.skills)
                    + ".",
                    {"name": {"type": "string", "enum": list(self.skills)}},
                    ["name"],
                )
            )

        return tools

    def prompt(self) -> str:
        """What the system prompt says about these tools."""
        return "\n".join(
            part for part in (catalog(self.skills), REFERENCE_GUIDANCE) if part
        )

    def run(
        self,
        name: str,
        arguments: dict,
        workspace: Path | None = None,
        case_root: Path | None = None,
    ) -> dict:
        """Run one of these tools. Failures come back as results, never raised.

        ``workspace`` and ``case_root`` are the session's folders, for the
        sandbox tools. A ``view_image`` result carries the picture under
        ``attach_image``, for the loop to send as a message the model can see.
        """
        handlers: dict[str, Callable[[dict], Any]] = {
            "reference_search": self._search,
            "reference_read": self._read,
            "load_skill": self._load_skill,
        }

        if self.ledger.offered:
            handlers["ledger_query"] = self.ledger.query
            handlers["notes_timeline"] = self.ledger.timeline
            handlers["notes_conflicts"] = self.ledger.conflicts

        if self.sandbox_problem is None and workspace is not None:
            handlers["exec"] = lambda arguments: run_command(
                self.sandbox, workspace, case_root, str(arguments.get("command") or "")
            )
            handlers["view_image"] = lambda arguments: image_for_model(
                workspace, str(arguments.get("path") or "")
            )

        if name.startswith("mcp__"):
            handlers[name] = lambda arguments: self.mcp.call(name, arguments, workspace)

        handler = handlers.get(name)

        if handler is None:
            return {"status": "error", "name": name, "message": f"Unknown tool: {name}"}

        try:
            result = handler(arguments)
        except LedgerNotReady as exc:
            return {"status": "error", "name": name, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - the model gets the failure, not a crash
            return {"status": "error", "name": name, "message": str(exc) or repr(exc)}

        # The reference library reports its own refusals as {"error": ...}.
        if isinstance(result, dict) and "error" in result:
            return {"status": "error", "name": name, "message": str(result["error"])}

        if name == "view_image":
            return {
                "status": "ok",
                "name": name,
                "result": {"path": result["path"], "shown": True},
                "attach_image": result,
            }

        # An MCP tool may return a picture too, such as a mask overlay.
        if isinstance(result, dict) and isinstance(result.get("attach_image"), dict):
            image = {**result["attach_image"], "path": "returned image"}
            output = {
                key: value for key, value in result.items() if key != "attach_image"
            }
            return {
                "status": "ok",
                "name": name,
                "result": output,
                "attach_image": image,
            }

        return {"status": "ok", "name": name, "result": result}

    def _search(self, arguments: dict) -> Any:
        return self.references.search(
            str(arguments.get("query") or ""),
            arguments.get("site"),
            int(arguments.get("limit") or 5),
        )

    def _read(self, arguments: dict) -> Any:
        url = str(arguments.get("url") or "")
        problem = self.outbound.problem(url)

        if problem:
            return {
                "error": f"That URL was not fetched: {problem}. Reference URLs "
                "name a topic, never the patient."
            }

        return self.references.read(
            url,
            arguments.get("section"),
            int(arguments.get("max_chars") or 8000),
        )

    def _load_skill(self, arguments: dict) -> Any:
        skill = self.skills.get(str(arguments.get("name") or ""))

        if skill is None:
            return {
                "error": f"Unknown skill. Skills: {', '.join(self.skills) or 'none'}"
            }

        result: dict[str, Any] = {
            "name": skill.name,
            "requires": skill.requires,
            "instructions": skill.body,
        }
        note = load_skill_note(
            skill,
            self._router_holds(),
            self.agents,
            self.mcp.tools,
            sandbox=self.sandbox_problem is None,
        )

        if note:
            result["note"] = note
            result["instructions"] = f"{skill.body}\n\n{note}"

        return result

    def _router_holds(self) -> set[str]:
        """Tools the router can call: every case tool, plus this object's own."""
        held = {tool["function"]["name"] for tool in TOOL_DEFINITIONS}
        held.update(self.names)
        return held
