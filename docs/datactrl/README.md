# datactrl

The controller: the only path from MedDataOS to a case's files. It opens a filesystem root, auto-discover DICOM / structured data / clinical notes, and fetch bounded slices for humans or LLM tools.

See [DESIGN.md](DESIGN.md) for scope.

## Quick start

```python
from meddataos.core.datactrl import Case

case = Case.open("/path/to/case")
report = case.discover()
print(report.to_inventory())  # compact ids/labels/counts for LLM tools
print(report.to_dict())       # full resource list

# Progressive disclosure: describe → fetch(view=...)
for ref in report.resources:
    print(case.describe(ref.id)["views"])

# Structured rows; describe gives column types, the table's shape, and its tests
for table in report.structured:
    print(case.describe(table.id)["summary"]["shape"])
    print(case.fetch(table.id, view="rows", limit=10))
labs = report.structured[0].id
case.fetch(labs, view="rows", where=[{"column": "test", "value": "AFP"}], order_by="-collected_at")
case.fetch(labs, view="series", item="AFP", from_date="2011-04-15")   # one unit, with a summary
case.fetch(labs, view="score", name="child_pugh", at="2011-03-10",
           inputs={"ascites": "none", "encephalopathy": "none"})       # missing inputs are listed, not guessed

# Notes (meta: note_date + confidence/flags, note_type, text_quality)
for note in report.notes:
    print(case.fetch(note.id, view="text", max_chars=2000))
    print(case.fetch(note.id, view="sections"))                     # headers + char offsets
    print(case.fetch(note.id, view="span", start=0, end=200, context_chars=100))
print(case.notes_search("portal vein thrombosis", limit=8))         # BM25 hits with offsets
# PDF notes: view="page", page_index=N returns one page (pdf_base64 + text + quality)

# DICOM: pick a series from inventory or dicom_tree / dicom_groups, then describe → fetch
for series in report.dicom:
    print(case.describe(series.id)["views"])  # preview omitted for RTPLAN/RTDOSE etc.
    print(case.describe(series.id)["links"])
print(case.fetch(report.dicom[0].id, view="metadata"))
print(case.fetch(report.dicom[0].id, view="preview", slice_index=0))
print(case.fetch(report.dicom[0].id, view="volume", spacing=[2, 2, 2]))       # base64 .nii.gz
print(case.fetch(report.dicom[0].id, view="volume", out_dir="/tmp/volumes"))  # a file there
print(case.dicom_groups("CT,SEG"))
```

## Volumes

`view="volume"` returns one gzip NIfTI, in one of two ways:

| Delivery | Ask with | Returns | Largest volume |
|---|---|---|---|
| Inline | nothing | `volume_base64` | 33.5 M voxels (512 × 512 × 128) and 64 MB |
| To a file | `out_dir=<folder>` | `volume_path`, the file in that folder | 268 M voxels (512 × 512 × 1024) and 1 GB |

Inline is for a caller that receives the volume over a transport; its limit keeps one message small. A caller on the same machine names a folder, and the volume is written there once, named by its content, so fetching it twice writes one file. The MedDataOS server does that in harness mode, with the session workspace's `volumes/` folder. A larger volume raises `LimitExceeded` with a hint to pass a coarser `spacing`. Limits are in `limits.py`.

## Links

Resources can be connected in two ways:

| Source | Origin | Persisted? |
|--------|--------|------------|
| `structural` | Adapters (e.g. DICOM SEG→CT references from imagetools) | Yes — derived from files |
| `asserted` | User or agent via `assert_link` | No — session-only overlay on the open `Case` |

Structural links appear automatically after discovery (see `case.describe(series_id)["links"]` for DICOM). Asserted links let you record cross-resource relationships the harness did not infer — for example, that a note mentions a lab table, or that two findings support the same hypothesis.

```python
case = Case.open("/path/to/case")
note = case.report.notes[0]
table = case.report.structured[0]

# Record a relationship between two known resources
link = case.assert_link(note.id, table.id, relation="mentions")
# → {"from_id": "...", "to_id": "...", "relation": "mentions", "source": "asserted", "meta": {}}

# Asserted links are merged into describe() for either endpoint
case.describe(note.id)["links"]   # includes the link above
case.links(note.id)               # structural + asserted for one resource
case.links()                      # case-wide links (DICOM structural + all asserted)
```

Notes:

- Both `from_id` and `to_id` must already exist in the discovery index; unknown ids raise `ResourceNotFound`.
- `relation` is a free-form label (`mentions`, `supports`, `same_patient`, …).
- Optional `meta={...}` attaches extra key/value data to the link.
- Asserted links are **not written to disk** — they live only for the lifetime of the `Case` object (or tool session). Re-open the case and they are gone.

Via tool dispatch (same semantics, JSON in/out):

```python
from meddataos.core.datactrl.tools import dispatch

dispatch("assert_link", {
    "root": "/path/to/case",
    "from_id": "note:abc123",
    "to_id": "structured:def456",
    "relation": "mentions",
})
dispatch("links", {"root": "/path/to/case", "resource_id": "note:abc123"})
```

## Tool dispatch

```python
from meddataos.core.datactrl.tools import TOOL_DEFINITIONS, dispatch

print(dispatch("discover", {"root": "/path/to/case"}))
# compact inventory; then: describe / fetch / notes_search / links / assert_link / dicom_tree / dicom_groups
# Gemini-style declarations: gemini_function_declarations()
```

## Install and tests

datactrl lives in `src/meddataos/core/datactrl` of the MedDataOS repository and is installed with it (`pixi install`, or `pip install -e ".[datactrl]"` from the repository root). It imports nothing else from MedDataOS, so the `datactrl` extra alone is enough to use it; `tests/core/test_layers.py` keeps it that way. Its tests are in `tests/core/datactrl`:

```bash
pixi run pytest tests/core/datactrl
```
