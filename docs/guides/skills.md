# Write a skill

A skill is domain know-how for the model: what to read for a kind of question, in what order, and how to phrase the claims. Skills are plain Markdown in `skills/<name>/SKILL.md`, so a clinician or domain lead can review and edit them without touching Python. Three ship with MedDataOS: `dicom`, `notes` and `tabular`.

A skill grants nothing. Which tools an agent may call is that agent's file. See [Define an agent](agents.md).

## The file

```markdown
---
name: pathology
description: Pathology reports - histology, grade, margins, stage, receptor status
requires: describe, fetch, notes_search, ledger_query
---
# Pathology

## How to work

- Find the pathology report with `notes_search` ("pathology", "surgical pathology").
- Read it with `fetch` `view=text` before answering anything about it.
- ...

## How to answer

- Quote the report's own wording for histology and grade.
- ...
```

- **`name`**: what the model and an agent call it.
- **`description`**: one line. The main model sees it in the skill catalog and decides from it when to call `load_skill`, so name the kinds of data and questions it covers.
- **`requires`**: the tools this know-how needs. It is checked, not granted: an agent that lists the skill has to grant every name here, or the server stops at startup. A pattern on the agent (`mcp__totalseg__*`) covers a specific tool the skill requires from that server. A long list may continue on indented lines, and every name counts:

  ```markdown
  requires: describe, fetch,
    exec, view_image
  ```

- **`uses`**: optional tools, such as `mcp__totalseg__*` and `mcp__mira-neck__*` on the DICOM skill. An agent does not have to grant them. When the router loads the skill and lacks one of these that an agent can use right now, the reply names that agent.
- **`tools`**: not read. A skill that still has `tools:` grants nothing. Startup warns, naming the file: grants belong in `agents/<name>.md`, and the skill's needs belong in `requires:`.
- **The body**: the instructions. The main model loads them with `load_skill`. An agent that lists the skill receives them already, in its system prompt.

The same layout is read by Claude Code and LangChain's Deep Agents, so skills can move between them.

## How it is used

The main model sees every skill's name and description, and when to call `load_skill`: for guidance it can follow with its own tools. The same prompt has one rule for when to launch an agent instead. See [How the router chooses](agents.md#how-the-router-chooses).

If it loads a skill and lacks a tool from `requires` or `uses` that an agent can use right now, the reply includes a note naming that agent. The note is based on what is available, not on the grant: with the sandbox off it does not say an agent can use `exec`. When no agent can use a missing tool, the note says why (the sandbox is off, the server is not connected, or no agent grants it). `load_skill` returns that list under `requires`.

An agent is separate. It names the skills it carries, and those bodies are already in its prompt, in the order it lists them. It does not call `load_skill` for them. [Define an agent](agents.md) is how that agent is granted tools and offered to the main model.

## Writing good instructions

- **Say what to read first**, and which view (`metadata`, `text`, `rows`, `volume`). Most wrong answers come from reading the wrong thing.
- **Say what not to do.** For example, the tabular skill forbids arithmetic in prose: values come from `series` summaries and `score` views.
- **Say how to cite** the domain's sources, especially derived values: what the method should name and which reads are its inputs.
- **Say when to abstain.** "If the files do not say, answer that they do not."
- **Test it on real questions.** Add eval questions for the domain and compare runs before and after a change ([Run the evals](evals.md)).
