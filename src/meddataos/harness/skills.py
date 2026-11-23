"""Skills: domain instructions as files, loaded when a question needs them.

Each skill is ``skills/<name>/SKILL.md``: a front matter block, then the
instructions in Markdown. The layout follows the Agent Skills convention that
Claude Code and LangChain's Deep Agents read, so skills stay portable.

    ---
    name: dicom
    description: Scans and series: metadata, structures, links, background.
    requires: dicom_tree, dicom_groups, describe, fetch,
      reference_search
    uses: mcp__totalseg__*, mcp__mira-neck__*
    ---
    How to work: ...

Only the name and description go into the system prompt. The model calls
``load_skill`` for the full text, so instructions for areas a question does not
touch cost nothing. ``requires`` lists the tools the know-how needs, and
``uses`` lists optional ones, such as a segmentation server. A value may
continue on indented lines. Neither grants anything: an agent's ``tools`` line
is the grant, and it has to cover ``requires``. A leftover ``tools:`` line is
ignored and warned about at startup.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from meddataos.core.workspace import REPO
from meddataos.harness.frontmatter import front_matter

SKILLS_DIR = REPO / "skills"


@dataclass
class Skill:
    """One skill: what it is for, the tools its know-how needs, and its text.

    ``requires`` is what an agent that lists the skill must grant. ``uses`` is
    optional: tools the know-how can call when they are offered, such as a
    segmentation server. Neither list grants anything.
    """

    name: str
    description: str
    body: str
    requires: list[str] = field(default_factory=list)
    uses: list[str] = field(default_factory=list)


def _comma_list(value: str) -> list[str]:
    return [name.strip() for name in value.split(",") if name.strip()]


def parse_skill(text: str, fallback_name: str) -> Skill:
    """A SKILL.md file's front matter and body.

    ``requires`` and ``uses`` are comma-separated lists. A value may continue
    on indented lines, so a wrapped list is not cut at the first line.
    """
    fields, body = front_matter(text)
    return Skill(
        name=fields.get("name") or fallback_name,
        description=fields.get("description", ""),
        body=body,
        requires=_comma_list(fields.get("requires", "")),
        uses=_comma_list(fields.get("uses", "")),
    )


def load_skills(
    folder: Path = SKILLS_DIR,
    on_warning: Callable[[str], None] | None = None,
) -> dict[str, Skill]:
    """Every skill under ``folder``, by name.

    A ``tools:`` line grants nothing. When ``on_warning`` is given, it is told
    once per such file, so startup can log it.
    """
    skills: dict[str, Skill] = {}

    if not folder.is_dir():
        return skills

    for path in sorted(folder.glob("*/SKILL.md")):
        text = path.read_text()
        fields, _body = front_matter(text)

        if on_warning is not None and "tools" in fields:
            on_warning(
                f"skills/{path.parent.name}/SKILL.md has tools:, which grants "
                "nothing; grants belong in agents/<name>.md and the skill's "
                "needs in requires:"
            )

        skill = parse_skill(text, path.parent.name)
        skills[skill.name] = skill

    return skills


def catalog(skills: dict[str, Skill]) -> str:
    """The system prompt's list of skills, or an empty string when there are none.

    When to load one, as against launching an agent, is the router's delegation
    rule. This line only names the skills and that ``load_skill`` is for
    guidance the router's own tools can follow.
    """
    if not skills:
        return ""

    entries = "; ".join(
        f"{skill.name}: {skill.description}" for skill in skills.values()
    )
    return (
        f"Skills: {entries}. Call load_skill when you will follow it with "
        "your own tools."
    )
