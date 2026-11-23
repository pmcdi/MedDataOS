---
name: dicom
description: Scans and series - acquisition metadata, dates, slice thickness and spacing, structures and their links, measurements, anatomy segmentation (vertebral levels, organ volumes, cervical lymph-node levels), and radiology background such as anatomy, classification and staging
requires: dicom_tree, dicom_groups, links, assert_link, describe, fetch, exec, view_image, reference_search, reference_read
uses: mcp__totalseg__*, mcp__mira-neck__*
---
# DICOM and radiology

## How to work

- Start with `dicom_tree` or `dicom_groups` to see the studies and series, then
  `describe` or `fetch` the series the question needs.
- `fetch` with `view=metadata` answers acquisition questions: dates, modality,
  slice thickness, pixel spacing, image count, structures and series links.
  Prefer it whenever it is enough.
- `fetch` with `view=preview` renders one slice, and `view=volume` returns a 3D
  NIfTI. The harness saves both in the session workspace and you get their
  `workspace_path`. Volumes on the native grid are fine: they go to a file, not
  into your context. On RTSTRUCT or SEG series, pass `roi=<structure name>`;
  measure on the native grid, since resampling can erase small structures.
  Pass a coarser `spacing` only to save time when the detail does not matter.
- Look at a preview with `view_image` and its workspace path.
- Quantitative work (volumes in mL, HU statistics, diameters, label statistics)
  is code run with `exec` in the sandbox. Write the script under `scripts/`, its
  outputs under `out/`, and run it with `python`. Fetched volumes are under
  `volumes/` in the workspace; the case is read-only at `/case`. There is no
  network. When `exec` is not offered, the sandbox is off: say the measurement
  needs it rather than estimating from the metadata.
- Anatomy the case's contours do not hold (vertebrae, spinal canal, thyroid,
  oesophagus, trachea, carotids, and about a hundred more) comes from
  TotalSegmentator when `mcp__totalseg__segment` is offered. Fetch the CT with
  `view=volume` on its native grid first (TotalSegmentator resamples it to
  1.5 mm itself), then call `segment` with that workspace path and the
  structures you need (`list_structures` gives the names). It finds anatomy,
  never tumours: a GTV comes from the RTSTRUCT. A
  structure cut by the scan edge has a partial volume; say so. To name a
  vertebral level, compare the structure's centroid with the vertebrae's
  centroids, not their extents, which overlap. A full-resolution run takes
  about a minute; `fast` is coarser, so use it only to look around.
- Which cervical lymph-node levels a node or GTVn lies in comes from mira-neck
  when `mcp__mira-neck__segment_neck_levels` is offered. Fetch the CT with
  `view=volume` on its native grid, then call `segment_neck_levels` with that
  workspace path as `image_path` (about five minutes on 12 CPUs, so once per
  question). The result is JSON, and `label_map` is the path of
  `NECK_LEVELS.nii.gz`. Fetch the structure from the RTSTRUCT with
  `view=volume` and `roi=<name>`, and pass that path as `mask_path` and the
  label map as `label_map_path` to `mcp__mira-neck__levels_of_structure`.
  Report each level's share, the part outside every level, and the level
  holding the centroid. It segments IA, and IB, II, III, IV, V and VIIA on
  each side. It does not segment level VI, so a laryngeal or glottic structure
  can sit outside every level; say so rather than naming a level it did not
  return. Levels are anatomy, not disease: a level is involved only if the
  case's own contours or reports put disease there.
- Use `reference_search` and `reference_read` for background the case does not
  hold: anatomy, LI-RADS and other classification systems, staging, typical
  findings. Search with general medical terms, never patient details.

## How to answer

- Cite every claim about this patient to the case, with the cite token from
  `describe` or `fetch`.
- Reference pages are background, not evidence about the patient. Name the page
  in prose, such as "per Radiology Assistant, LI-RADS", with no `[cite:]` marker.
- Cite the fetch of the structure or series you measured (the volume, or a
  `view=preview` with `roi=<structure>` and `overlay=true`) as well as the exec
  run: the page shows that cited slice, so the reader sees what was measured.
- A value computed with `exec` is a derived citation: `cite` is the exec
  call's token, `result.value` is the number exactly as the script printed it,
  `inputs` names the fetch that gave the volume and the exec call, and `method`
  says how it was computed. Print the final values your answer gives; the server
  refuses a number the cited run did not print.
- A value from TotalSegmentator is a derived citation too: `cite` is the
  segment call's token, `result.value` the number as it returned it, `inputs`
  the CT volume fetch and the segment call, and `method` names the model and
  version it reported, such as "TotalSegmentator 2.18.0, task total".
- A value from mira-neck is a derived citation too: `cite` is the
  `levels_of_structure` call's token, `result.value` the share or volume as it
  returned it, `inputs` the CT fetch, the structure fetch and both mira-neck
  calls, and `method` the model and version it returned, such as
  "mira-neck 0.1.1".
- For a measurement, state the method, the voxel spacing, the units and any
  resampling.
- Say plainly when the images cannot answer the question.
