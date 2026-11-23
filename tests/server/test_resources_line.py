"""The system prompt's list of case files, shortened for the harness."""

from __future__ import annotations

from meddataos.server import dispatch
from meddataos.server.state import state


def files(dicom_count: int) -> list[dict]:
    note = [
        {
            "id": "note:1",
            "modality": "note",
            "label": "clinic",
            "web_path": "/case/n.txt",
        }
    ]
    series = [
        {
            "id": f"dicom:{index}",
            "modality": "dicom",
            "label": f"MR {index}",
            "web_path": f"/case/s{index}",
        }
        for index in range(dicom_count)
    ]
    return note + series


def test_a_long_run_of_series_is_counted_in_harness_mode(monkeypatch):
    monkeypatch.setattr(dispatch.case, "files", lambda: files(50))
    monkeypatch.setattr(state, "harness", True)

    line = dispatch.resources_line()

    assert '"note:1"' in line and "dicom:0" not in line
    assert "DICOM: 50 series, not listed above." in line


def test_a_few_series_and_an_external_agent_keep_the_full_list(monkeypatch):
    monkeypatch.setattr(dispatch.case, "files", lambda: files(50))
    monkeypatch.setattr(state, "harness", False)
    external_agent = dispatch.resources_line()

    monkeypatch.setattr(dispatch.case, "files", lambda: files(3))
    monkeypatch.setattr(state, "harness", True)
    few = dispatch.resources_line()

    assert "dicom:49" in external_agent and "DICOM:" not in external_agent
    assert "dicom:2" in few and "DICOM:" not in few
