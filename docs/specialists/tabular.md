# Tabular specialist

The tabular specialist answers from the case's tables: lab results, vitals, medications, encounters, treatment logs, and registry rows. A model decides which reads to make. Code computes every number, and the server checks every cited cell and computed value against the file.

```bash
pixi run tabular --case /path/to/case
```

It prints `http://127.0.0.1:8004/v1`. Register it in `configs/agents.toml`:

```toml
[tabular]
url = "http://127.0.0.1:8004/v1"
description = "Answers from the case's tables: lab results, vitals, medications, encounters, treatment logs, and registry rows. Use this for a value on a date, a trend, a lowest or highest value, a change over a period, and scores (Child-Pugh, ALBI, MELD, FIB-4, BMI, days between dates). Every number is computed by code from the table."
modalities = ["structured"]
```

It reads tables through the server's `fetch`, so the server records each read and can check what is cited from it. It is not a stand-alone `--agent-url`.

## What it can read

datactrl types each table when it is first described: every column gets a type (`number`, `date`, `category`, `text`, `mixed`, or `empty`), and the table gets a shape.

| Shape | Layout | Example |
|---|---|---|
| `long` | One row per test per draw, with test, value, unit, and date columns | A lab system export |
| `wide` | One row per visit, with numeric columns | Vitals, weekly toxicity reviews |
| `intervals` | Start and stop dates | A medication list |
| `events` | Dated rows without measurements | Encounters |
| `record` | One row | A registry or research record |
| `table` | Anything else | |

Three fetch views answer from a table:

| View | Returns |
|---|---|
| `rows` | Rows as they are. `where` filters with `eq`, `ne`, `gt`, `ge`, `lt`, `le`, `contains`, or `in`, `order_by` sorts, and `row_numbers` gives each row's position in the file. A date without a time covers the whole day. A column not in the table is an error. |
| `series` | One measurement over time, by `item` on a long table or `column` on a wide one, bounded by `from_date` and `to_date`. Values are brought to one unit, and a result a corrected result replaced is left out and listed under `superseded`. The summary gives first, last, lowest, highest, change, percent change, and the first value above or below the reference range. |
| `score` | `child_pugh`, `albi`, `meld`, `fib4`, `bmi`, or `interval`, at the draw nearest `at` within `window_days` (default 14). An input the table does not hold is taken from `inputs`. A score with a missing input is returned incomplete, with the list of what is missing. It is never estimated. |

Unit conversion covers only the conversions listed in `datactrl/utils/table.py`, such as albumin g/L to g/dL and bilirubin µmol/L to mg/dL. A value in a unit with no listed conversion is kept out of the summary and listed under `other_units`.

## How it answers

1. With no reads yet, a model sees the catalog of the case's tables and the question, and names the reads to make, up to eight. A read the catalog does not allow is dropped.
2. With the reads back, a model writes one claim per finding and names the read and the cells each claim rests on. A claim that states a change, a count, or a score is marked computed and gives the value. A read that failed is shown with its error, so one more round of reads can correct it.
3. Code keeps a claim only when its read exists, every number and date in it is in that read, and it names cells of that read that are not empty. A computed value has to be one the read returned.
4. A score that is incomplete becomes `needs`, such as `ascites: none, mild, or moderate to severe`. The orchestrator asks the specialist that can supply the missing input, then asks the tabular specialist again with that answer as context.

Each kept claim becomes a citation: `table` with its `cells`, or `derived` with the score's formula as `method` and each input, with its row and date or "given with the question", under `inputs`. The reply also carries `facts`, the keyed value behind each cited cell (`lab.afp`, `vital.weight`, `social_history.smoking`), so the orchestrator can compare it with the notes.

It uses `GEMINI_API_KEY`, or the Azure settings, from `.env`, like the other specialists.
