---
name: tabular
description: The case's tables - lab results, vitals, medications, encounters, treatment logs and registry rows; a value on a date, trends, lowest or highest, changes, and scores (Child-Pugh, ALBI, MELD, FIB-4, BMI, days between dates)
requires: describe, fetch
---
# Tables

Every number in the answer comes out of a read. You never compute: the server
refuses a table-cited sentence that holds a number or date no cited read
returned.

## How to work

- `describe` a table first: its shape, its columns and, for a lab table, the
  tests it holds.
- A lab value: `fetch` with `view=series` and `item=<test>`, the test name as the
  table lists it, without its unit. It returns each result with its date,
  reference range and flag, plus first, last, min, max, change and percent
  change. A rows read of a lab table has none of those.
- A wide table (vitals): `view=series` with `column=<column>`.
- A period (after surgery, during treatment): bound the series with `from_date`
  and `to_date`. If you do not know the dates, read the table that holds them
  first.
- Rows as they are (a registry row, a medication, an encounter): `view=rows`
  with `where`, `order_by`, `columns` and `limit`, using the table's own column
  names.
- A score: `view=score` with `name` (child_pugh, albi, meld, fib4, bmi,
  interval) and `at=YYYY-MM-DD`. Lab inputs are read at the draw nearest `at`.
  Pass `inputs` only for what a table cannot hold and the question or the notes
  state, such as `{"ascites": "none", "encephalopathy": "none"}` for Child-Pugh
  or `{"age": 71}` for FIB-4. Never guess an input: an incomplete score comes back
  naming what it needs, so find it in the notes or say it is missing.

## How to answer

- Each sentence gives the value, its unit and its date, followed by the cite
  token of the read it came from.
- A lowest, highest, first or last value is a row of the series: cite it as a
  table citation with that row. A change, percent change, count, number of days
  or score is a derived citation whose `result.value` is exactly what the read
  returned.
- A result marked superseded was corrected: report the value that replaced it,
  and mention the superseded one only if the question asks.
- State an H or L flag or a reference range only as the read gives it.
- When the tables do not hold what was asked, say so in plain words ("not in
  the tables").
