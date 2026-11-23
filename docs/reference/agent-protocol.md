# Agent protocol

What the server sends an external agent at `--agent-url`, and what the agent must send back. [Use an external agent](../guides/external-agent.md) shows how to run one.

An agent is an HTTP server that accepts `POST {base}/chat/completions` and returns an OpenAI chat completion. `base` is the part before `/chat/completions`, as passed to `--agent-url`.

## Requests

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

## Answers

The answer must meet the [answer format](answer-format.md). A refused answer comes back to the agent as described there, at most six times.

## The example agent

`pixi run agent` runs `examples/agent.py`. It listens on `127.0.0.1:8001` unless you pass `--host` and `--port`, and it accepts only `POST /v1/chat/completions`.

It replaces `model` with `OPENAI_MODEL` and posts `messages`, plus `tools` and `response_format` when the request includes them, to `OPENAI_BASE_URL`. When `OPENAI_BASE_URL` is unset it uses `https://api.openai.com/v1`. `OPENAI_API_KEY` is sent as a bearer token when set. The example agent's own wait is 120 seconds. `AGENT_MODEL`, `AGENT_API_KEY`, and `AGENT_TIMEOUT` apply to the MedDataOS server's call, which this forwarder does not read.

Set `OPENAI_MODEL`, or `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, and `AZURE_OPENAI_DEPLOYMENT`. Otherwise the example agent exits. Point `--agent-url` at another endpoint when you do not want this forwarder.
