# Troubleshooting

Find the message you see, then the cause and the fix. The server's terminal output usually says more than the page.

## Answers

### "The agent failed: … refused the tool list … set HARNESS_MODEL_API=responses"

The model deployment does not accept function tools on chat completions. Set `HARNESS_MODEL_API=responses` in `.env` and restart. See [Connect a model](models.md#chat-completions-or-responses).

### "The agent failed: … answered 400: … reasoning_effort …"

The deployment rejects the reasoning effort in `.env`. Some reject `none` or `minimal`; try `low`, or leave `AZURE_OPENAI_REASONING_EFFORT` empty.

### "The conversation no longer fits in the model's context window"

The question's reads outgrew the model's context, usually after many DICOM reads. Ask a narrower question, or start a new conversation. A model with a larger context window also helps.

### "The agent could not produce an answer with valid citations. Last problem: …"

The model failed the citation checks seven times in a row. The last problem says which rule it broke. Common ones:

| Problem says | Usually means |
|---|---|
| a quote is not in the note | The model paraphrased instead of copying a passage |
| a number is not one the cited call returned | It did arithmetic in prose, or cited the wrong read |
| a value is not the cell's | It misread a table row |
| markers side by side or at the end | It grouped citations instead of placing them after each claim |

Ask again, more narrowly, or use a stronger model. Repeated failures on one kind of question suggest the skill for that domain needs clearer instructions.

### "The agent failed: no answer after 16 steps"

The model kept calling tools without answering. Narrow the question. For DICOM work, raise the dicom agent's step budget in `agents/dicom.md`, or override it with `[agent.dicom]` `max_steps` in `configs/harness.toml`.

### "The agent failed: … did not answer" or "could not reach …"

The model endpoint timed out or is not reachable. Check the endpoint and key in `.env`. Rate limits and server errors are retried automatically (`HARNESS_MODEL_RETRIES`); a refused connection is not, because it means nothing is listening at that address.

### The answer says it cannot measure something

The sandbox is off, so the model can read but not compute. See [Turn on the sandbox](sandbox.md).

## Startup

### "Sandbox: the sandbox is off (set HARNESS_SANDBOX=docker)"

Expected until you turn it on. See [Turn on the sandbox](sandbox.md).

### "Sandbox: Docker has no 'runsc' runtime"

gVisor is not installed or not registered with Docker. Install it, or set `HARNESS_SANDBOX_RUNTIME=runc` on a development machine knowing it isolates less ([details](sandbox.md#without-gvisor)).

### "no sandbox Python at …"

Run `pixi run dicom-sandbox-install`.

### "agents/dicom.md grants mira-neck, which is not registered here; its tools are not offered"

The agent is allowed to use that MCP server, but this checkout has not registered it. Startup continues. That server's tools are not offered until you register it, for example in `configs/mcp.local.json`, and it connects. A grant is a permission, not a requirement that the server be installed. See [Define an agent](agents.md#grants).

### "skills/…/SKILL.md has tools:, which grants nothing"

A skill does not grant tools. `tools:` in `SKILL.md` is ignored. Move the grant to `agents/<name>.md`, and list what the know-how needs under `requires:`. See [Write a skill](skills.md).

### "… max_steps must be at least 1"

`max_steps` was `0` in the agent file or in `[agent.<name>]`. Use a positive integer. The message is the same for both.

### A file in the folder is missing from the case

The startup output lists each file it could not classify, with the reason. Unsupported formats stay unknown rather than being guessed at.

## MCP servers

### An MCP server is not connected

Check its status:

```bash
curl -s http://127.0.0.1:8080/api/health
```

| Status | Fix |
|---|---|
| `connecting` | Wait: a container server can take several seconds to start. |
| *(not listed)* | It is not registered, or registered under another name. `pixi run mcp list`. |
| `failed: refused to run outside a container: …` | Docker or the runtime is missing. Same fix as the sandbox runtime above. |
| `failed: no environment at …` | Install its pixi environment, such as `pixi install -e totalseg`. |
| `failed: mount source … does not exist` | A path in its `mounts` is missing, such as the TotalSegmentator weights. |
| `awaiting approval: it comes from … and is not approved here` | A project server you have not approved. `pixi run mcp get <name>`, then `pixi run mcp trust <name>`. |
| `awaiting approval: … changed since it was approved` | Its entry in `configs/mcp.json` was edited. Review the change, then trust it again. |
| `failed: image … is not on this machine` | Build or pull the image; it is never pulled automatically. |
| `refused: its tools differ from the trusted ones …` | The server's tools changed since you trusted it. Review the change, then `pixi run mcp trust <name>`. |
| `failed: the server ran out of memory (limit …)` | The container was killed. Raise `memory` in its registration; the limit in the message is the one it ran with. It is not retried. |
| `failed: …; retrying in N s` | It crashed or could not start; the server keeps retrying. Its error follows `failed:`. |
| `… (gave up)` | It failed six times in a row. Fix the cause and restart the server. |

### TotalSegmentator gives a smaller volume than expected

Check whether the result says the scan edge cuts the structure: its volume is then only the part inside the scan. See [Set up TotalSegmentator](totalsegmentator.md#things-to-know).
