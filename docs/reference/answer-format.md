# Answer format

The exact contract an answer must meet, for anyone building an agent or debugging a refusal. [Cited answers](../concepts/citations.md) explains the ideas behind it.

`describe` and `fetch` record the resource they were called with, when that id is in the case file list. Those are the opened files. Other tools do not open a file. A failed call does not either.

If no file was opened, the answer is shown when it is plain text and contains neither `[cite:` nor `"citations"`. A JSON object in that case is refused with a request for plain text. Text that does contain a citation marker is checked with the rules below, against every discovered file.

If a file was opened, the whole answer must be one JSON object that matches `CitedAnswer` in `src/meddataos/core/verifier/schema.py`:

```json
{
  "response": "The planning CT shows the larynx [cite:c1]. The contours outline it [cite:c2]. Treatment finished on February 13 [cite:c3]. The dose was 51 Gy in 20 fractions [cite:c3].",
  "citations": [
    {
      "kind": "imaging",
      "cite": "c1",
      "summary": "Axial planning CT of the neck. The larynx is included on the displayed slice."
    },
    {
      "kind": "imaging",
      "cite": "c2",
      "summary": "RTSTRUCT for that planning CT. The larynx contour is named and outlined on the same slice.",
      "fields": ["roi_names"]
    },
    {
      "kind": "note",
      "cite": "c3",
      "agent": "clinical_notes",
      "summary": "Radiation oncology completion note dated 13 February. External-beam treatment finished that day, 51 Gy in 20 fractions, with follow-up arranged in radiation oncology.",
      "evidence": [
        {
          "n": 1,
          "claim": "Treatment finished on February 13",
          "quote": "Treatment completed February 13"
        },
        {
          "n": 2,
          "claim": "The prescribed dose was 51 Gy in 20 fractions",
          "quote": "51 Gy in 20 fractions"
        }
      ],
      "conflicts": [
        {
          "attribute": "rt.end_date",
          "severity": "high",
          "rationale": "The completion note and the follow-up note give different end dates.",
          "values": [
            {"value": "February 13", "note_label": "rt-completion", "start": 120, "end": 151},
            {"value": "February 20", "note_label": "follow-up", "note_id": "note:followup", "start": 40, "end": 51}
          ]
        }
      ]
    }
  ]
}
```

Each citation has a `kind`, and the kind follows from the call its token came from: `imaging` for a DICOM series, `note` for a clinical note, `table` for a table, and `file` for a path cited without a token. An imaging citation has only `cite`, `summary`, and `fields`. A finding from a note is a separate note citation, with the token from that note's own fetch. When the claim is a calculation or prediction from a call (for example a structure volume from a volume fetch), use `kind` `derived` with that call's cite token, a plain-language `method`, a `summary`, and `inputs`. Each input is an object with `cite` (a token from this question), `label` (what was taken from that call), and optional `detail`. `result` (`value` / `unit`) and `detail` on the citation stay optional. The server refuses an imaging citation that carries `evidence`, `agent`, or `conflicts`, and any citation whose kind does not match its call (except `derived`, which is allowed on any cite token). When `kind` is missing, the server uses the kind of the call.

A table citation may name `cells`: each is `{"row": …, "column": …, "value": …}`. `row` is the row's position in the file, as `row_numbers` on a rows fetch or `row` on a series point gives it. On a series fetch `column` may be left out. The server refuses a cell the call did not return, an empty cell, and a value that is not the cell's. When `value` is left out the server fills it in. A derived citation of a table call must give `result.value`, and that value has to be one a table call it names returned: a number from a series summary (first, last, lowest, highest, change, percent change) or a score. Rounding may move it by at most half its last digit, and by at most 5 percent. The server refuses a value worked out from cells by hand.

A delivered table citation carries `table`, the server's record of the call: the cited rows of a rows fetch, every point and the summary of a series, or the inputs, points, and result of a score. The page draws its chart and marks its cells from that record, not from what the agent wrote.

The system message includes the JSON schema of `CitedAnswer`, with its descriptions and examples. After the first `describe` or `fetch` in a question, the router's requests also send a strict copy of that schema as `response_format` (`type: json_schema`, `strict: true`), so a model that supports structured outputs replies in this shape. If the endpoint answers 400 or 422, the server retries that conversation without `response_format`, and then without tools. The schema checks the shape. The server still checks the references: the token is a call from this question, each quote is in the note, and where the markers sit.

Put `[cite:token]` right after the words that call supports. A sentence may cite several calls, but the markers cannot sit side by side or be grouped at the end of the answer. `cite` is the token on the `describe` or `fetch` result, and the marker uses that same token. The server fills in `id`, `file`, `web_path`, and the command before the page sees the reply. The summary is two or three sentences of what that call shows: the finding, the date, and the measurement, so a reviewer can check the card without opening the file. A DICOM metadata citation may include `fields`, the metadata keys that support the claim. The values stay on the server. A `derived` citation's `method` is one or two sentences of how the value was obtained (input call, ROI or series, resampling or model, and the arithmetic or prediction step), readable without opening code. Read a note with `fetch` `view=text`. A citation of that call must include `evidence` with `quote`, a contiguous passage copied from the `text` that fetch returned. Several claims from one fetch are several evidence items on that one citation, each with its own `quote` from that same text, and the same `[cite:token]` goes after each of those claims. The server finds each passage, sets `start` and `end`, and fills `span_url`. It refuses a quote that is not in the note, and it refuses a quote that is the whole note. When notes disagree, the citation also sends `conflicts`. An agent that did not call those tools cites `web_path` instead, with an `id` such as `1`, and that path must be a file in the open case. It may send `span_url` itself.

A refused answer is appended to the conversation, followed by a user message that starts with `The server refused that answer.`, states the problem, repeats the rule and the example, and lists the opened `web_path` values. The server does this at most six times after the original answer. The next failure is shown on the page as `The agent could not produce an answer with valid citations. Last problem: …`.

Anything other than a chat completion (`choices[0].message` as an object), a connection error, or a timeout is shown as `The agent failed: …`.
