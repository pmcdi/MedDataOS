# Imaging specialist

The imaging specialist ranks the case's series against the question, renders one preview slice, and describes that slice with a vision model. It is a stand-in until a fuller imaging agent exists. Any endpoint that answers the same way can replace it.

```bash
pixi run imaging --case /path/to/case
```

It prints `http://127.0.0.1:8003/v1`. Register it beside notes:

```toml
[imaging]
url = "http://127.0.0.1:8003/v1"
description = "Looks at one DICOM slice. Use this for questions about the images, scans, contours, or what is visible on imaging."
```

A confident pick becomes one citation for the series file, tagged `imaging`, with a `preview` of the slice index, window, and overlay. The page loads that slice from `/api/dicom/preview`. Contours are drawn for an RTSTRUCT, or when the question asks for them. If no series ranks confidently, the reply cites nothing and lists the ranked series ids under `needs`. A consult that passes one of them in `resource_ids` reads that series.

The router calls it as `consult_imaging`. Its citation becomes a finding, such as `imaging.1`, with the slice record the page loads, and `basis` `model_read`, not checked: one slice described by a vision model is not a verified read. The contract is in [Connect an agent](../agents.md#imaging-specialist-stand-in).

It reads `GEMINI_API_KEY`, or the Azure settings, from `.env`. An Azure deployment used for imaging has to accept images.
