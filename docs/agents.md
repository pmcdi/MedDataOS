# Connect an agent

An agent is an HTTP server that accepts `POST {base}/chat/completions` and returns an OpenAI chat completion. Pass `base`, the part before `/chat/completions`, when you start MedDataOS:

```bash
pixi run serve /path/to/folder --agent-url http://127.0.0.1:8001/v1
```

The server will not start without `--agent-url`. For each question it sends the chat, runs any tool calls on the open folder, and checks the answer before the page shows it.

## What the server sends

Each request is JSON:

| Field | Value |
|---|---|
| `model` | `AGENT_MODEL` from `.env`. Empty when that variable is unset. |
| `messages` | The system message, then the session history. |
| `tools` | The datactrl tools, omitted on the retry described below. |

`AGENT_API_KEY`, when set, is sent as `Authorization: Bearer …`. `AGENT_TIMEOUT` is how many seconds the server waits for each response. The default is `120`.

If the response has `tool_calls`, the server runs them on the open case and sends the results back as `tool` messages, up to sixteen rounds. The patient-card request also allows sixteen rounds. When a response has no tool calls, its content is the answer. A markdown code fence around JSON is removed.

If the citations are wrong, the server sends the agent's answer back with the reason and asks for a corrected one. After six corrections, or if the agent errors or times out, the page shows a short failure message instead of waiting.

The system message for a question contains:

- instructions to read the files with the tools and to ground the answer in them,
- the citation rule and a JSON example,
- `Case resources:` followed by a JSON list of discovered files.

Each resource in that list has `id`, `modality` (`note`, `structured`, or `dicom`), `label`, and `web_path`. `web_path` is the file's URL on this server, such as `/case/clinic_note.txt`. Paths are URL-encoded per segment. The list is the `resources` array from the discovery report.

The tools are `discover`, `describe`, `fetch`, `notes_search`, `links`, `assert_link`, `dicom_tree`, and `dicom_groups`. Their schemas come from datactrl. `root` and `case_id` are removed from every schema, including `required`. The server drops those two arguments if the agent sends them, and sets `root` to the folder it opened before it runs the tool.

A tool result comes back as a `tool` message whose content is JSON:

```json
{"status": "ok", "name": "fetch", "result": {}}
```

A tool that raises, or arguments that are not a JSON object, come back as `{"status": "error", "name": "…", "message": "…"}` so the agent can try again. The server does not stop the question for that.

The server calls the agent again with those tool messages, up to sixteen completions. If the sixteenth completion still contains `tool_calls`, the page shows `The agent failed: no answer after 16 steps`.

If the endpoint answers `400` or `422` to a request that includes `tools`, the server retries that request once without `tools` and does not send tools again for the rest of that question. The agent can then answer only from the messages it has already been given.

When a completion has no `tool_calls`, its `content` is the answer. The server removes one markdown fence: a leading ` ``` ` line and a trailing ` ``` `. If the remaining text is a JSON object whose `citations` field is missing or empty, and `message`, `response`, or `text` is a non-empty string, that string replaces the object. An object that has citations is left as JSON for the check below.

## Patient card

When the server starts, it sends a separate completion. The system message asks for one JSON object and includes the same case resource list. The user message is `Fill in the patient card.`

The object may contain:

| Key | Kept when |
|---|---|
| `name`, `age`, `sex` | A string or number. Numbers are stored as text. |
| `blood_type` | Same. A blank value is stored as empty. |
| `allergies`, `conditions` | A list. Only non-empty strings are kept. |
| `data_dates` | An object whose values are `YYYY-MM-DD`. Other values are dropped. |

A markdown fence around that JSON is removed. Any other shape, invalid JSON, or a failed request leaves the card as the folder name. The sidebar already shows that name, and the server pushes the filled card to open pages when the reply is usable. Notes, Tables, and DICOM on the card come from the discovery report. `data_dates` is shown beside a citation when the cited path is `/case/<folder>/…` and `<folder>` is a key in `data_dates`.

## More than one agent

`--agent-url` stays the router: the only agent that talks to the page. Specialists are other chat endpoints the router can call. Register them in a TOML file:

```toml
[notes]
url = "http://127.0.0.1:8002/v1"
description = "Answers from clinical notes with verified quotes, dates, and conflicts."
modalities = ["note"]

[dicom]
url = "http://127.0.0.1:8005/v1"
description = "DICOM and radiology specialist. ..."

[tabular]
url = "http://127.0.0.1:8004/v1"
description = "Answers from the case's tables: a value on a date, a trend, and scores."
modalities = ["structured"]
```

```bash
pixi run serve /path/to/folder --agent-url http://127.0.0.1:8001/v1 --agents configs/agents.toml
```

`--specialist notes=http://127.0.0.1:8002/v1` does the same for one agent, and can be repeated. A name is a short lowercase word. The same name cannot be listed twice.

### Consulting a specialist

The router is given one extra tool per specialist, `consult_notes`, `consult_tabular`, and so on. The description is the routing hint. The call takes:

| Argument | Meaning |
|---|---|
| `question` | What the specialist should answer. Required. |
| `context` | What other specialists found that this one needs. |
| `resource_ids` | Case resource ids the specialist should use. |

The server runs the call. It posts the question and the datactrl tools to the specialist. Context and resource ids follow the question in the same message, under the lines `Context from other specialists:` and `Use these resources:`. A specialist is not given a `consult_` tool, so specialists cannot call each other.

Consult calls sent in one turn run at the same time. A specialist can be consulted up to three times per question, and a question can make eight consults in all. The same call made twice gets the first answer and does not count. Past the budget the call returns a tool error. A timeout, an HTTP error, or citations that still fail after six corrections also become a tool error, and the router can still answer from the specialists that succeeded.

### What a specialist returns

A specialist may answer in plain text, or with the citation JSON below. A `describe` or `fetch` it calls comes back with a `cite` token, and that token is what the citation names. A specialist that reads the case itself, without those tools, cites the file's `web_path` instead.

Beside `response` and `citations`, the JSON may carry:

| Key | Value |
|---|---|
| `abstentions` | A list of what the specialist's files do not say. |
| `needs` | A list of what it lacks to answer. Each item is `{"what": …, "why": …}`, with `options` when the caller can choose, such as series ids. |
| `facts` | The keyed values behind its citations. Each item is `{"key": "lab.afp", "value": …, "unit": …, "date": "YYYY-MM-DD", "cite": the marker it belongs to}`, with optional `assertion` and `source_type`. |

A reply with needs or abstentions and nothing to cite has an empty `citations` list, and is checked as plain text.

### Findings

The server checks a specialist's reply once, when it arrives. Each usable citation becomes a finding with an id: `notes.1`, `notes.2`, `tabular.1`. The server fills the citation in as it does for the router (file, `web_path`, command, DICOM slice, table record, quote offsets) and keeps it.

The consult call returns:

```json
{
  "status": "ok",
  "name": "consult_notes",
  "result": "{\"response\": \"Treatment finished on 13 February [cite:notes.1].\", \"citations\": [...]}",
  "findings": [{"id": "notes.1", "text": "Treatment finished on 13 February [cite:notes.1].", "kind": "file", "file": "2006-02-13-rt-completion.txt", "agent": "clinical_notes"}],
  "abstentions": ["The reason for the two-day gap is not documented."],
  "needs": [],
  "facts": [{"key": "rt.end_date", "value": "2006-02-13", "finding": "notes.1"}]
}
```

`result` is the specialist's reply with its markers renamed to finding ids. Its citations carry only `id`, `kind`, `file`, `web_path`, `agent`, and `summary`.

The router cites a finding by its id: it puts `[cite:notes.1]` after the words the finding supports. It does not copy the citation. Before the reply is checked, the server puts its own checked citation behind every finding the reply cites, with `finding` set to the id. A citation the router sends for a finding id is dropped. `citations` then lists only files the router read itself, and is empty when every marker names a finding. A plain-text reply may cite findings the same way.

Three things follow from that:

- A marker shaped like a finding id that no specialist returned is refused.
- A read a specialist made cannot be cited by the router with that read's cite token. The token belongs to the specialist.
- When specialists returned findings and the router opened nothing itself, the router cannot cite a case file by `web_path`. It cites the finding.

A router may also send `discrepancies`: the values different findings give for one attribute. Each item has `attribute`, `values` (each with `value` and the `finding` it came from, and optional `unit`, `date`, and `side`), and optional `kind`, `severity`, `rationale`, and `preferred` (a finding id). The server keeps an item when at least two of its values name different findings it holds, adds each value's specialist and file, and adds any of those findings' citations the answer does not already carry.

### Who reads which files

A specialist may list `modalities` it owns: `note`, `structured`, or `dicom`. Each modality has at most one owner. For a file of an owned modality, the router's own `describe` and `fetch` return a tool error that names the specialist to consult. `notes_search` does the same when a specialist owns `note`. What the page shows for those files is then what the specialist found and the server checked.

If the owning specialist fails on a question, the router may read its files for the rest of that question. The patient-card request is not held to ownership. A specialist with no `modalities` owns nothing, and the router may read those files as before.

Every delivered citation says what it rests on, under `basis`:

| Field | Values |
|---|---|
| `type` | `quote` (a passage of a note), `cell` (table cells), `computed` (a value code worked out from a table), `model_read` (a model's description of an image), `metadata`, or `file`. |
| `checked` | `server` when this server compared the evidence with the file, `specialist` when only the specialist did, `none` otherwise. |
| `by` | `router`, or the specialist's name. |

A citation that names no agent is given the one for its file: `clinical_notes`, `imaging`, or `structured`. `basis.by` says whether the router or a specialist read it.

With no specialists registered, none of this is added and the server talks only to `--agent-url`.

## Clinical notes specialist

The notes specialist is `pixi run notes --case /path/to/case` from the root of this repo (it runs `python -m meddataos.specialists.notes.serve`). `pixi run stack` starts it with the rest. It prints an address such as `http://127.0.0.1:8002/v1`. For notes alone, pass that address as `--agent-url`. When more than one specialist is running, keep a router as `--agent-url` and register the notes process beside it:

```toml
[notes]
url = "http://127.0.0.1:8002/v1"
description = "Answers from clinical notes with verified quotes, dates, and conflicts. Use this for questions about the chart, history, labs in notes, and discrepancies between notes."
modalities = ["note"]
```

It opens that case itself and returns citation JSON. Each citation is tagged `clinical_notes` and carries the verified quotes and documented discrepancies, which the page shows on the badge, the hover card, and the source viewer. It does not ask the server to read files, so its quotes reach the page with `basis.checked` set to `specialist`: the notes specialist located and entailment-checked them, and the server did not re-read the note.

Beside its citations it sends `abstentions` (what the notes do not document) and `facts` (the ledger facts behind each cited claim, keyed by attribute such as `social_history.smoking`). Context from a consult is shown to its model to frame the question. It is never cited, and every claim is still verified against the notes.

## DICOM specialist

The DICOM specialist is `pixi run dicom`. It is built on the OpenAI Agents SDK and reads the case only through the datactrl tools the server sends. It can also search allowlisted radiology reference sites and run Python in a sandbox on fetched volumes. See [DICOM specialist](specialists/dicom.md).

## Tabular specialist

`pixi run tabular --case /path/to/case` starts it on `http://127.0.0.1:8004/v1`. Register it with `modalities = ["structured"]`. It reads tables through the server's `fetch`, so it is not a stand-alone `--agent-url`. [Tabular specialist](specialists/tabular.md) describes how it answers.

## The orchestrator

`pixi run orchestrator` serves `meddataos.orchestrator.serve` on `http://127.0.0.1:8001/v1`, and `pixi run stack` starts it as the router. It reads the same model settings as the example agent. With no specialists registered, each request goes to the model as it came, as with the example agent.

With specialists, each question runs as steps in code. The server holds the conversation, so each request carries the question so far and the orchestrator keeps no state between requests.

| Step | Who decides |
|---|---|
| Plan | A model writes a stand-alone question for each specialist that holds part of the answer. Code sends them as consult calls in one turn. If the plan fails, every specialist is asked the question as written. A message that needs no patient data, such as a greeting, is answered without consulting. |
| Reconcile | Code. Findings, abstentions, needs, and errors are gathered. A finding a specialist repeats when asked again is kept once. The keyed facts of every specialist go into one store, and the values sources disagree on become `discrepancies`. |
| Gap check | Code first: a specialist whose need another specialist has answered is asked again with those findings as `context`, for the part that was waiting only. A model is asked only when a need has no supplier yet, or the first round left something undocumented. It says whether a finding in hand already answers the need, which specialist could supply it, or which option to pick. Questions already asked are not repeated, and the per-specialist budget is respected. Consulting stops after three rounds. |
| Write | A model writes the answer from the findings, citing each by id. Code repairs markers that sit side by side or stand alone, drops markers for findings that do not exist, and checks that every number and date in a cited sentence is in the finding it cites. One rewrite is asked for. After that, or after the server refuses twice, the answer is the specialists' own sentences, stitched in code. |

Sources that disagree are compared with the notes specialist's conflict logic (`meddataos.specialists.facts`): a static attribute across all dates, a time-varying one on the same date only, numbers within measurement tolerance. Within one source, disagreement is left to that source. When more sources record one value than any other, that value is preferred. Otherwise the more reliable source is: pathology; then a lab table or final radiology; then operative notes or image measurements; then physician notes, preliminary radiology, or chart tables; then nursing phone notes; then research forms or registry rows.

## Azure OpenAI

The example router and the orchestrator speak Azure when these are set in this repo's `.env`. The notes, DICOM, and tabular specialists read the same variables from the same file:

```bash
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_ENDPOINT=https://YOUR_RESOURCE.openai.azure.com
AZURE_OPENAI_DEPLOYMENT=your-deployment-name
AZURE_OPENAI_API_VERSION=2024-10-21
```

`AZURE_OPENAI_DEPLOYMENT` is the deployment name. `AZURE_OPENAI_API_VERSION` is the date Azure puts on the request. When the key, endpoint, and deployment are set, those calls do not use Gemini. When they are empty, the specialists keep using `GEMINI_API_KEY`.

Some reasoning models reject function tools on chat completions unless `reasoning_effort` is `none`, and the page then gets no tools. Set `AZURE_OPENAI_REASONING_EFFORT=none` for those (`OPENAI_REASONING_EFFORT` when not using Azure). Leave it unset for models that don't take the parameter.

## Citations

`describe` and `fetch` record the resource they were called with, when that id is in the case file list. Those are the opened files. Other tools do not open a file. A failed call does not either.

If no file was opened, the answer is shown when it is plain text and contains neither `[cite:` nor `"citations"`. A JSON object in that case is refused with a request for plain text. Text that does contain a citation marker is checked with the rules below, against every discovered file.

If a file was opened, the whole answer must be one JSON object that matches `CitedAnswer` in `src/meddataos/server/answer.py`:

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

Put `[cite:token]` right after the words that call supports. A sentence may cite several calls, but the markers cannot sit side by side or be grouped at the end of the answer. `cite` is the token on the `describe` or `fetch` result, and the marker uses that same token. The server fills in `id`, `file`, `web_path`, and the command before the page sees the reply. The summary is two or three sentences of what that call shows: the finding, the date, and the measurement, so a reviewer can check the card without opening the file. A DICOM metadata citation may include `fields`, the metadata keys that support the claim. The values stay on the server. A `derived` citation's `method` is one or two sentences of how the value was obtained (input call, ROI or series, resampling or model, and the arithmetic or prediction step), readable without opening code. Read a note with `fetch` `view=text`. A citation of that call must include `evidence` with `quote`, a contiguous passage copied from the `text` that fetch returned. Several claims from one fetch are several evidence items on that one citation, each with its own `quote` from that same text, and the same `[cite:token]` goes after each of those claims. The server finds each passage, sets `start` and `end`, and fills `span_url`. It refuses a quote that is not in the note, and it refuses a quote that is the whole note. When notes disagree, the citation also sends `conflicts`. A specialist that did not call those tools cites `web_path` instead, with an `id` such as `1`, and that path must be a file in the open case. It may send `span_url` itself.

A refused answer is appended to the conversation, followed by a user message that starts with `The server refused that answer.`, states the problem, repeats the rule and the example, and lists the opened `web_path` values. The server does this at most six times after the original answer. The next failure is shown on the page as `The agent could not produce an answer with valid citations. Last problem: …`.

Anything other than a chat completion (`choices[0].message` as an object), a connection error, or a timeout is shown as `The agent failed: …`.

## The example agent

`pixi run agent` runs `examples/agent.py`. `pixi run stack --router example` uses it in place of the orchestrator. It listens on `127.0.0.1:8001` unless you pass `--host` and `--port`, and it accepts only `POST /v1/chat/completions`.

It replaces `model` with `OPENAI_MODEL` and posts `messages`, plus `tools` and `response_format` when the request includes them, to `OPENAI_BASE_URL`. When `OPENAI_BASE_URL` is unset it uses `https://api.openai.com/v1`. `OPENAI_API_KEY` is sent as a bearer token when set. The example agent's own wait is 120 seconds. `AGENT_MODEL`, `AGENT_API_KEY`, and `AGENT_TIMEOUT` apply to the MedDataOS server's call, which this forwarder does not read.

Set `OPENAI_MODEL`, or `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, and `AZURE_OPENAI_DEPLOYMENT`. Otherwise the example agent exits. Point `--agent-url` at another endpoint when you do not want this forwarder.
