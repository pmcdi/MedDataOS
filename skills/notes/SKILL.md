---
name: notes
description: Clinical notes - history, findings, treatment and dates, with verbatim quotes, and the places the notes disagree
requires: notes_search, describe, fetch, ledger_query, notes_timeline, notes_conflicts
---
# Clinical notes

## How to work

- Start from the ledger when it is offered: `ledger_query` finds compiled facts
  by attribute prefix (`tumor.`, `lab.afp`, `social_history.`) or by words,
  `notes_timeline` gives events in date order, and `notes_conflicts` lists where
  the notes disagree. Each fact has its note id, the exact quote and its
  character offsets.
- The ledger finds things; it is not evidence. Read the source with `fetch`
  (`view=span` at a fact's offsets, or `view=text` for a short note) and cite
  that read.
- Without the ledger, or for anything it missed, use `notes_search`, then fetch
  the sections it points to.
- Cover every part of the question and every documented item it asks for (all
  risk factors, all sites, all treatments), not only the most prominent one.

## How to answer

- Every claim about the patient is a note citation: the cite token of the read,
  and an evidence item whose quote is copied verbatim from that read.
- Give values with units and dates. Say when a date is inferred or uncertain.
- For "today", "now" or "current", give the last documented value with its date;
  never extrapolate past the record.
- Negated findings are findings: write "no portal vein thrombosis" when the note
  says so.
- When the notes disagree on something the question touches, give each side with
  its own quote, say which source is more reliable (a final report over a
  preliminary read, pathology over imaging for histology), and add a `conflicts`
  entry to the note citation. Use the ledger's attribute name, such as
  `tumor.size_cm` or `social_history.smoking`, and list each value with its note.
- When part of the question is not in the notes, say so in plain words ("not
  documented in the notes"). Never fill a gap from general knowledge.
