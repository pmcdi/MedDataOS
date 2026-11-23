# Connect a model

MedDataOS works with any model that takes function tools over the OpenAI chat completions or Responses API. Set it in `.env`; the server reads it at startup.

## Azure OpenAI

```bash
AZURE_OPENAI_ENDPOINT=https://YOUR_RESOURCE.openai.azure.com
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_DEPLOYMENT=your-deployment-name
AZURE_OPENAI_REASONING_EFFORT=low      # optional; leave empty to send none
```

When the endpoint and key are set, Azure is used and the `OPENAI_*` settings are ignored. The notes ledger uses Azure too.

## OpenAI, or any compatible server

```bash
OPENAI_MODEL=gpt-...
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=                       # empty means https://api.openai.com/v1
```

For a local server, point `OPENAI_BASE_URL` at it; most need no key:

```bash
OPENAI_BASE_URL=http://127.0.0.1:11434/v1   # Ollama
OPENAI_MODEL=llama3.1
```

A gateway such as the LiteLLM proxy works the same way, which is how to reach providers that do not speak the OpenAI API.

## Chat completions or Responses

`HARNESS_MODEL_API` picks the API:

- `chat` (default): `/chat/completions`. Works with nearly everything.
- `responses`: `/responses`. Needed by deployments that refuse function tools on chat completions, which some Azure reasoning deployments do at every reasoning effort. The request is stateless (`store: false`): the whole conversation is sent each time and nothing is kept by the provider.

If the server stops a question with "The model refused the tool list … set HARNESS_MODEL_API=responses", that is this case.

## Choosing a model

The harness relies on tool calls and on following a strict answer format, so capable tool-using models work best. Smaller local models can run it but fail citations more often, which shows up as more corrections and slower answers. The evals tell you how a model does on real questions: see [Run the evals](evals.md).

## Per-agent models

Each agent can use its own model or reasoning effort. Set it in `configs/harness.toml` so the shared agent file stays free of one deployment:

```toml
[agent.dicom]
model_api = "responses"
model = "your-bigger-deployment"
reasoning_effort = "medium"
max_steps = 24
```

See [Harness config](../reference/harness-config.md). The file's own `max_steps` is what the DICOM agent ships with (24). A table here overrides it.

## Timeouts and retries

- `HARNESS_MODEL_TIMEOUT` (default 120): seconds to wait for one model response.
- `HARNESS_MODEL_RETRIES` (default 4): retries after a rate limit (429), a server error (5xx), a dropped connection or a timeout. The wait follows the provider's `Retry-After` when it sends one, otherwise it backs off, never more than 60 seconds. A refused request (400) or a server that is not listening is reported at once.
