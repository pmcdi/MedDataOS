# Cited answers

Every claim in an answer points at something the model read during that question, and the server checks that the claim matches what it read. This page explains what that means in practice. The exact JSON is in [Answer format](../reference/answer-format.md).

## Reads and cite tokens

When the model calls `describe` or `fetch`, the server records what came back and gives the read a token: `c1`, `c2`, and so on. The model cites by token, `[cite:c1]`, right after the words that read supports. Because the server holds the record, it can check the claim against the file instead of trusting the model's account of it.

Only reads count. A file the model never opened cannot be cited, and if it opened no file at all, it must answer in plain text without citations.

## What gets checked

| Source | The claim must | The page shows |
|---|---|---|
| A clinical note | Quote a passage copied exactly from the text that read returned. The server finds it and highlights it. A quote of the whole note is refused. | The note, with the passage highlighted |
| A table | Name cells (row, column, value) the read returned, and a number in the sentence must be one of them | The table or a chart, with the cells marked |
| A DICOM series | Describe what the read showed; metadata citations may name the fields they rest on | The slice or metadata |
| A computed value | Name how it was computed and which reads went in. The value must be one the cited run actually returned | The inputs and the method |

Two rules apply to every answer:

- **Numbers must come from the cited reads.** In a sentence cited only to reads with recorded output (table reads, sandbox runs, TotalSegmentator), every number must be one those reads returned, allowing for rounding. "AFP rose from 8.2 to 27.3" passes only if the read returned both values. The model is not allowed to do arithmetic in its head and present the result as data.
- **Markers go where the claim is.** They cannot be bunched at the end of the answer.

## Derived values

Some answers need a computation: a tumour volume, an ALBI score, a change over time. These are cited as **derived**, with:

- the method in plain words ("voxel count of the GTVp mask times voxel volume");
- the inputs, each a read from this question;
- the result value and unit.

A derived value must be one that a tool returned: a score or series summary from the controller, a number a sandbox script printed, or a measurement TotalSegmentator reported. A value worked out by hand from cells is refused.

## What is not evidence

Some tools help the model find things but are never evidence about the patient:

- **Reference sites** give background (what LI-RADS LR-5 means, typical anatomy). The model names the page in prose, without a marker.
- **The notes ledger** points to facts in the notes. The model then reads the passage itself with `fetch` and cites that read.
- **Navigation** tools (search, DICOM trees, links) lead to a read; the read is what gets cited.

A tool nobody declared is treated as background, so a new tool can never become evidence by accident. The system prompt's citing rule is generated from the same declarations the verifier uses, so the model is told exactly what will pass. [Tools](../reference/tools.md) lists every tool's class.

## What the checks do not cover

The verifier proves that a claim came from the source it cites. It does not prove the source is right, and it does not check the meaning of prose without numbers or quotes: "no nodal disease [cite:c3]" passes if `c3` is a real read of the right kind. A model's reading of an image is marked with `!` on the page because nothing can check it. A computed value shows that the tool returned it, not that the tool's method suits the question. Review the cited sources for anything that matters.
