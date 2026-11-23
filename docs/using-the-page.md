# Use the page

The page is where you ask questions, check answers and keep notes on a case. This covers what each part does.

## Open a case

```bash
pixi run serve /path/to/folder
```

Any folder works. The server walks it and sorts what it finds into clinical notes (text, PDF), tables (CSV, Excel) and DICOM series; nothing needs a particular layout or manifest. It prints how many of each it found, and lists anything it could not classify, so check that output when a file seems to be missing.

One server opens one folder, and the page shows that folder as the only patient. To open another case, stop the server and start it on the other folder. Chats, pins and reviews are saved per folder and come back when you reopen it ([where they are stored](reference/storage.md)).

The page is at the URL the server prints, `http://127.0.0.1:8080/?patient=<folder name>` by default. `HOST` and `PORT` in `.env` change the address.

## The sidebar

The patient's name is the folder name until the model has read the files and filled in the patient card: name, age, sex, blood type, allergies and conditions. Hover the name to see the card and how many notes, tables and DICOM series the case holds.

Below it are the conversations for this case. **+ New** starts an empty one; each is named after its first question.

## Asking

Type a question and press Enter (Shift+Enter for a new line). Questions work best when they name what you want and from where, for example:

- "What was the tumour size on the pathology report?"
- "How did AFP change between the first and last draw?"
- "Which structures are contoured on the planning CT?"
- "At which vertebral level is the primary tumour?"

While the question runs, the answer bubble shows what is happening: a moving bar, the current activity (such as `Reading clinic_note.txt` or `Waiting for dicom`), a timer, and one row per step with the file it read and its cite token. When a subagent is working, its own steps are grouped under it. Once the answer arrives the trace collapses to a line such as `Worked for 14s · 4 files read`; click it to open it again.

The paperclip attaches a `.csv` or `.xlsx` for the question. It is kept apart from the case folder and replaced by the next upload.

## Reading an answer

An answer is text with a marker after each claim, such as `Notes c1`. The marker says what kind of source it is and which read it came from.

- **Hover a marker** to preview the source: the note with the quoted passage highlighted, the table with the cited cells marked (a series as a chart), or the DICOM slice. Click it for a larger view.
- **The marker's border tells you how far to trust it.** A check after the token means the server verified the claim against the file: the quote is in the note, the cell holds that value, the number is one the read returned. A dashed border means nothing verified it. A `!` marks a model's reading of an image, which no code can check.
- **Derived values** (a volume, a score, a computed change) name how they were computed and which reads went in. A value from the sandbox or TotalSegmentator is checked against what that run actually printed.
- **Disagreements.** When notes disagree, for example two different treatment end dates, a card under the answer lists each value, its source, which one is preferred and why.

If an answer says the files do not hold something, that is deliberate: the model is told to say so rather than guess, and it cannot cite a file it did not read.

When the model cannot produce citations that pass after six corrections, the page says `The agent could not produce an answer with valid citations` and gives the last problem. See [Troubleshooting](guides/troubleshooting.md).

## The timeline

Above the chat, a strip shows the case's dated files in lanes: notes, imaging, and one lane per table. It is built from the dates in the files, with no model involved, and dates are not reconciled. A note whose date the controller is unsure of is drawn dashed. The dates the latest answer rests on are ringed. Click a mark to open that note or table row.

## Reviews and pins

- **Review checklists.** A citation summary with several parts becomes a checklist in the source dialog. Tick items as you verify them; the ticks are saved with the case.
- **Pins.** Pin a citation, or select text in an answer and pin it. Pins collect in the right-hand panel, where you can add a note and tags (`#follow-up`, `#critical`, `#reviewed`, `#discuss`, `#normal`). Clicking a pin jumps back to it in its conversation.

## The audit export

**Export audit** downloads the current conversation's audit runs as JSON: for each question, its steps, every file read and by whom, each refused attempt and why, the delivered answer and its citations. File contents and quotes are left out. [Storage](reference/storage.md) describes the format and how to query it directly.

The saved chats and audit runs contain patient questions and answers. Treat the `data/` folder as patient data.
