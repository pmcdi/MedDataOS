# Run the evals

The evals ask a fixed set of questions with known answers through the running server, and score each answer. Use them to compare models, settings, skills or code changes on the same questions.

!!! warning "Evals call your model"
    Every question is a real conversation with the configured model, so a run costs tokens and time. A full tabular run (10 questions) took about 3 minutes and 440k input tokens, most of them served from the prompt cache.

## Run them

```bash
pixi run eval --label my-change                 # every area
pixi run eval --area tabular --label my-change  # one area: notes, tabular or dicom
pixi run eval --only tab-meld,tab-bmi           # chosen questions
```

For each case the runner starts the server in harness mode, waits for it, asks that case's questions (each in a new conversation), and stops it. Results go to `.cache/evals/<label>-<time>/`:

- `summary.json`: passed, cited and errors per area, mean seconds per answer, and tokens (input, output, cached);
- `results.jsonl`: every answer, with its verdict;
- the server's log.

`results.jsonl` holds answers about the cases, so it stays in `.cache`, out of git.

To score an external agent instead, pass its launch command:

```bash
pixi run eval --label agent --launch "python -m meddataos.server {case} --agent-url http://127.0.0.1:8001/v1"
```

## The cases

| Area | Questions | Case |
|---|---|---|
| `tabular` | 10 | `SYN-EVAL-01`, synthetic, in `evals/cases/` |
| `notes` | 35 | `TCGA-DD-A4NF`, `RADCURE-0006` |
| `dicom` | 9 | `TCGA-DD-A4NF`, `RADCURE-0006` |

The real cases are not in the repository. The runner looks for them under `data/`, or the folder in `MEDDATA_CASES` or `--cases`, and skips the questions of a case it cannot find. Questions marked `segmentation` need [TotalSegmentator](totalsegmentator.md).

## Add a question

Questions are in `evals/questions/<area>.json`:

```json
{
  "id": "dicom-radcure-gtv-volume",
  "patient_id": "RADCURE-0006",
  "type": "quantitative",
  "question": "What is the volume of the primary gross tumour (GTVp)?",
  "expect": [],
  "expect_number": {
    "min": 1.9,
    "max": 2.7,
    "units": ["ml", "cm3", "cm³", "cc", "cubic"],
    "why": "Filled-pixel count gives 2.25 mL and polygon area 2.13 mL; an earlier specialist run reported 2.592 cm3. The range admits reasonable rasterisations."
  }
}
```

- **`expect`** is a list of groups; the answer must match one alternative in every group. An alternative is a lowercase substring, `re:<regex>`, or `date:YYYY-MM-DD` (matched in the usual spellings). For example `[["re:\\b17\\b"]]` for a MELD of 17.
- **`expect_number`** passes when a number in the range is written next to one of the units, as above.
- **`expect_abstain: true`** passes when the answer says the files do not hold it.
- **`expect_conflict`** passes when the answer reports a disagreement on that attribute, such as `"tumor.size_cm"`.
- **`why`** records where the expected answer comes from, so someone can check it later.

Derive the expected answer independently of MedDataOS (from the file, or a separate calculation), and record how in `why`. An expected value copied from a MedDataOS answer only tests that it repeats itself.
