# MedDataOS

MedDataOS answers questions about one patient's files. Point it at a folder of clinical notes, lab tables and DICOM studies, open the page in your browser, and ask. Each answer comes back with citations to the files it used, and the server checks every citation against what was actually read before you see it.

Everything runs on your machine. The only thing that leaves it is the conversation with the model you choose: Azure OpenAI, OpenAI, or a local server such as Ollama or vLLM.

## What makes it different

- **Answers are checked, not trusted.** A quote must appear in the note it cites. A lab value must be the cell it names. A number in a sentence must be one the cited read returned. An answer that fails goes back to the model with the reason, up to six times. [Cited answers](concepts/citations.md) explains the rules.
- **One audited path to the data.** The model reads the case only through the controller (datactrl), so every file it opened is recorded against the question and saved in an audit log.
- **Tools that fit the data.** Beyond reading files, the model can run code in a sandbox, look up radiology references, search a ledger of facts from the notes, segment anatomy with TotalSegmentator, and hand parts of a question to focused subagents.

## Where to start

| You want to | Read |
|---|---|
| Try it on the sample case in ten minutes | [Get started](getting-started.md) |
| Ask questions and check the answers | [Use the page](using-the-page.md) |
| Understand what happens to a question | [How it works](concepts/how-it-works.md) |
| Connect your model, turn on the sandbox, add TotalSegmentator | [Guides](guides/models.md) |
| Look up a setting, a tool or the answer format | [Reference](reference/settings.md) |
| Change the code | [Code map](development/architecture.md) |

Something not working? See [Troubleshooting](guides/troubleshooting.md).
