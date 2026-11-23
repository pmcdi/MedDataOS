# Get started

This walks you through asking your first question, on a synthetic case that ships with the repository. It takes about ten minutes, most of it installing.

## What you need

- [pixi](https://pixi.sh), which installs Python and every dependency into the project.
- A model that takes function tools: an Azure OpenAI deployment, an OpenAI API key, or a local OpenAI-compatible server such as Ollama or vLLM.
- Optional, for later: Docker, for the code sandbox and TotalSegmentator.

## 1. Install

From the repository root:

```bash
pixi install
```

## 2. Tell it which model to use

Copy the example settings and fill in one model:

```bash
cp .env.example .env
```

=== "Azure OpenAI"

    ```bash
    AZURE_OPENAI_ENDPOINT=https://YOUR_RESOURCE.openai.azure.com
    AZURE_OPENAI_API_KEY=...
    AZURE_OPENAI_DEPLOYMENT=your-deployment-name
    ```

=== "OpenAI"

    ```bash
    OPENAI_MODEL=gpt-...
    OPENAI_API_KEY=sk-...
    ```

=== "Local (Ollama, vLLM)"

    ```bash
    OPENAI_BASE_URL=http://127.0.0.1:11434/v1
    OPENAI_MODEL=llama3.1
    ```

If your deployment refuses function tools on chat completions (some Azure reasoning deployments do), also set `HARNESS_MODEL_API=responses`. [Connect a model](guides/models.md) has the details.

## 3. Open the sample case

```bash
pixi run serve evals/cases/SYN-EVAL-01
```

`SYN-EVAL-01` is a made-up hepatology patient: one clinic note, a lab table and a vitals table. The server prints what it found and a URL:

```text
Discovered 3 resources (notes=1, tables=2, dicom=0, unknown=0)
...
Web UI: http://127.0.0.1:8080/?patient=SYN-EVAL-01
```

Open that URL.

## 4. Ask a question

Type:

> What was the highest AFP and when was it drawn?

While it works, the answer bubble shows each step: which files it read, and how long each took. After a few seconds you should get an answer saying AFP peaked at 42 on 2020-07-22, with a marker after the claim.

Hover the marker. It shows the lab table with the cited row highlighted, which is the server's own record of that read, not text the model wrote. A check mark on the marker means the server verified the value against the file.

## 5. Try a few more

These all have known answers in the sample case:

- How did AFP change from the first draw to the last?
- What was the MELD score on 2020-07-22?
- What was the Child-Pugh score at the October 2020 visit?
- What was the hemoglobin A1c? (The files do not say. A good answer says so instead of guessing.)

## Next

- [Use the page](using-the-page.md): the trace, the timeline, pins, reviews and the audit export.
- [Open your own case](using-the-page.md#open-a-case): any folder of notes, tables and DICOM works; no layout is required.
- [Turn on the sandbox](guides/sandbox.md) so the model can measure volumes and run calculations.
