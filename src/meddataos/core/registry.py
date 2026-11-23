"""Every tool's evidence class: how the verifier treats what the tool returns.

MedDataOS checks each answer's citations against what its tools returned, so it
has to know which outputs are evidence about the patient:

- ``case``: a read of the case (``describe``, ``fetch``). Citable by cite token.
- ``derived``: a value computed from case reads (``exec``; later, model servers
  such as segmentation). Citable as a derived citation; the call's output is
  recorded and the cited value must be in it.
- ``background``: material about medicine, not this patient (reference sites).
  Named in prose, never cited.
- ``navigation``: finds things to read (the notes ledger, ``dicom_groups``,
  ``load_skill``). Not citable; the model cites the read it leads to.
- ``control``: runs other work (``spawn_subagent``), which returns findings.

A tool nobody registered is ``background``: a new tool can never become evidence
by accident.
"""

from __future__ import annotations

from enum import StrEnum


class Evidence(StrEnum):
    """How the verifier treats a tool's output."""

    CASE = "case"
    DERIVED = "derived"
    BACKGROUND = "background"
    NAVIGATION = "navigation"
    CONTROL = "control"


_REGISTRY: dict[str, tuple[Evidence, str]] = {}


def register(name: str, evidence: Evidence, source: str) -> None:
    """Record a tool's evidence class and where it comes from.

    Registering a name twice with a different class is a mistake worth stopping
    on: two sources would disagree about whether a result can be cited.
    """
    known = _REGISTRY.get(name)

    if known is not None and known[0] != evidence:
        raise ValueError(
            f"tool {name!r} is {known[0]} from {known[1]}, not {evidence} from {source}"
        )

    _REGISTRY[name] = (evidence, source)


def evidence_of(name: str) -> Evidence:
    """The tool's evidence class; background when nobody registered it."""
    known = _REGISTRY.get(name)
    return known[0] if known else Evidence.BACKGROUND


def source_of(name: str) -> str | None:
    """Where a registered tool comes from: datactrl, harness, or an MCP server."""
    known = _REGISTRY.get(name)
    return known[1] if known else None


def citing_rule(offered: list[str]) -> str:
    """The prompt's sentence on which of the offered tools can be cited."""
    case_reads = [name for name in offered if evidence_of(name) is Evidence.CASE]
    derived = [name for name in offered if evidence_of(name) is Evidence.DERIVED]
    background = [name for name in offered if evidence_of(name) is Evidence.BACKGROUND]

    parts = [f"Cite only these: case reads ({', '.join(case_reads)})"]

    if derived:
        parts.append(f"and, as derived values, runs of {', '.join(derived)}")

    rule = " ".join(parts) + "."

    if background:
        rule += (
            f" {', '.join(background)} give background, never evidence about "
            "the patient: name the source in prose."
        )

    return rule + " Other tools help you find what to read."


def registered() -> dict[str, Evidence]:
    """Every registered tool and its class."""
    return {name: evidence for name, (evidence, _) in _REGISTRY.items()}


# The case tools from datactrl.
for _name in ("describe", "fetch"):
    register(_name, Evidence.CASE, "datactrl")

for _name in (
    "discover",
    "notes_search",
    "links",
    "assert_link",
    "dicom_tree",
    "dicom_groups",
):
    register(_name, Evidence.NAVIGATION, "datactrl")

# The harness's own tools.
register("exec", Evidence.DERIVED, "harness")
register("reference_search", Evidence.BACKGROUND, "harness")
register("reference_read", Evidence.BACKGROUND, "harness")

# A preview is cited through the fetch that made it; looking at it is not a read.
for _name in (
    "view_image",
    "load_skill",
    "ledger_query",
    "notes_timeline",
    "notes_conflicts",
):
    register(_name, Evidence.NAVIGATION, "harness")

register("spawn_subagent", Evidence.CONTROL, "harness")
