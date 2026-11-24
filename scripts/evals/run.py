"""Ask every eval question through a running MedDataOS server and score the answers.

    pixi run eval --label harness
    pixi run eval --area tabular --only tab-meld,tab-bmi
    pixi run eval --label agent \
        --launch "python -m meddataos.server {case} --agent-url URL"

For each case the runner starts the ``--launch`` command (the server in harness
mode by default), waits for the page's health check, asks that case's questions
one per new session, and stops it. Talking to the server over HTTP means any
launch command can be scored on the same questions.

Results go to ``.cache/evals/<label>-<time>/``: ``results.jsonl`` (answers
included, so it stays out of git with the rest of ``.cache``), ``summary.json``
and the server's log.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .scoring import check, load_questions, parse_answer, verdict

REPO = Path(__file__).resolve().parents[2]
QUESTIONS = REPO / "evals" / "questions"
SYNTHETIC_CASES = REPO / "evals" / "cases"
RESULTS = REPO / ".cache" / "evals"
DEFAULT_LAUNCH = f"{sys.executable} -m meddataos.server {{case}} --harness"
POLL_SECONDS = 1.0


def case_folder(patient_id: str, cases_dir: Path) -> Path | None:
    """The synthetic case in the repo, else the folder under ``cases_dir``."""
    for candidate in (SYNTHETIC_CASES / patient_id, cases_dir / patient_id):
        if candidate.is_dir():
            return candidate

    return None


def http_json(url: str, form: dict[str, str] | None = None) -> dict[str, Any]:
    """GET, or POST a form when one is given, and decode the JSON reply."""
    data = urllib.parse.urlencode(form).encode() if form is not None else None

    with urllib.request.urlopen(url, data=data, timeout=30) as response:
        reply: dict[str, Any] = json.load(response)

    return reply


def wait_until_healthy(
    base_url: str, process: subprocess.Popen, seconds: float
) -> bool:
    """Whether the server answers its health check before the time runs out."""
    deadline = time.time() + seconds

    while time.time() < deadline:
        # A launch that exits early (a busy port, a missing key) will never answer.
        if process.poll() is not None:
            return False

        try:
            http_json(f"{base_url}/api/health")
            return True
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(POLL_SECONDS)

    return False


def start_server(
    launch: str, folder: Path, port: int, log_path: Path
) -> subprocess.Popen:
    """Start the launch command for one case folder, logging to log_path."""
    # Split before substituting so a case path with spaces stays one argument.
    command = [
        folder.as_posix() if part == "{case}" else part for part in shlex.split(launch)
    ]
    log_file = log_path.open("a")

    # A new session puts the server and every process it starts in one group,
    # so stopping the group stops them all.
    return subprocess.Popen(
        command,
        cwd=REPO,
        env={**os.environ, "PORT": str(port), "PYTHONUNBUFFERED": "1"},
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def stop_server(process: subprocess.Popen) -> None:
    """Stop the server and every process it started."""
    if process.poll() is not None:
        return

    # SIGINT first, so the server shuts down cleanly.
    os.killpg(process.pid, signal.SIGINT)
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def ask(
    base_url: str, item: dict[str, Any], patient_id: str, timeout: float
) -> dict[str, Any]:
    """One question in a fresh session: the delivered answer and how long it took."""
    started = time.time()
    reply = http_json(
        f"{base_url}/api/chat", {"message": item["question"], "patient_id": patient_id}
    )

    if reply.get("status") != "processing":
        return {"error": reply.get("message") or "chat refused", "seconds": 0.0}

    session_url = f"{base_url}/api/session/{reply['session_id']}"
    while time.time() - started < timeout:
        session = http_json(session_url)

        if not session.get("processing"):
            answers = [
                entry
                for entry in session["history"]
                if entry.get("role") == "assistant"
            ]
            final = answers[-1] if answers else {}
            return {
                "content": final.get("content", ""),
                # Tool names, not display labels, so runs of the two paths compare.
                "steps": [
                    step.get("name") or step.get("label")
                    for step in final.get("steps") or []
                ],
                "seconds": round(time.time() - started, 1),
                # Tokens by who asked; only harness models report them so far.
                "usage": final.get("usage") or {},
            }

        time.sleep(POLL_SECONDS)

    return {
        "error": f"no answer within {timeout:.0f} s",
        "seconds": round(time.time() - started, 1),
    }


def score(item: dict[str, Any], asked: dict[str, Any]) -> dict[str, Any]:
    """One results row: the question, its checks, and whether it passed."""
    row = {
        key: item.get(key) for key in ("id", "area", "type", "patient_id", "question")
    }
    row["seconds"] = asked.get("seconds")

    if "error" in asked:
        return {**row, "passed": False, "error": asked["error"]}

    answer = parse_answer(asked["content"])
    checks = check(item, answer)
    return {
        **row,
        "passed": verdict(item, checks),
        "checks": asdict(checks),
        "citations": len(answer.citations),
        "steps": asked["steps"],
        "usage": answer_tokens(asked.get("usage") or {}),
        "response": answer.response,
    }


TOKEN_KEYS = ("calls", "input", "output", "cached")


def answer_tokens(usage_by_party: dict[str, Any]) -> dict[str, int]:
    """One answer's tokens, summed over who asked (router, each subagent)."""
    return add_tokens(
        [counts for counts in usage_by_party.values() if isinstance(counts, dict)]
    )


def add_tokens(counts: list[dict[str, Any]]) -> dict[str, int]:
    """Token counts added together."""
    return {key: sum(int(item.get(key) or 0) for item in counts) for key in TOKEN_KEYS}


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pass counts, citations, errors and mean time, per area and overall."""
    by_area: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_area[row["area"]].append(row)

    def totals(group: list[dict[str, Any]]) -> dict[str, Any]:
        timed = [row["seconds"] for row in group if row.get("seconds")]
        return {
            "passed": sum(1 for row in group if row["passed"]),
            "total": len(group),
            "cited": sum(1 for row in group if row.get("citations")),
            "errors": sum(1 for row in group if row.get("error")),
            "mean_seconds": round(sum(timed) / len(timed), 1) if timed else None,
            "tokens": add_tokens([row.get("usage") or {} for row in group]),
        }

    return {
        "areas": {area: totals(group) for area, group in sorted(by_area.items())},
        "overall": totals(rows),
        "failed": [row["id"] for row in rows if not row["passed"]],
    }


def print_summary(summary: dict[str, Any]) -> None:
    """Print the summary as a small table."""
    print(
        f"\n{'area':10s} {'passed':>8s} {'cited':>6s} {'errors':>7s} {'mean s':>7s}"
        f" {'input tok':>10s} {'cached':>9s} {'output tok':>10s}"
    )

    for area, totals in [*summary["areas"].items(), ("overall", summary["overall"])]:
        passed = f"{totals['passed']}/{totals['total']}"
        tokens = totals["tokens"]
        print(
            f"{area:10s} {passed:>8s} {totals['cited']:>6d} "
            f"{totals['errors']:>7d} {totals['mean_seconds'] or '-':>7} "
            f"{tokens['input']:>10,d} {tokens['cached']:>9,d} {tokens['output']:>10,d}"
        )

    # Zero tokens means nobody reported them, not that the run was free.
    if not summary["overall"]["tokens"]["calls"]:
        print("\n(no token usage reported: the model or agent did not send any)")

    if summary["failed"]:
        print("\nfailed:", ", ".join(summary["failed"]))


def main() -> int:
    """Run the selected questions case by case and write the results."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--label", default="run", help="Name for this run's results folder"
    )
    parser.add_argument(
        "--launch",
        default=DEFAULT_LAUNCH,
        help="Command that serves one case; {case} is its folder",
    )
    parser.add_argument(
        "--cases",
        default=os.getenv("MEDDATA_CASES", str(REPO / "data")),
        help="Folder holding the real cases",
    )
    parser.add_argument(
        "--area", default="", help="Comma-separated areas: notes, tabular, dicom"
    )
    parser.add_argument("--only", default="", help="Comma-separated question ids")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    parser.add_argument(
        "--timeout", type=float, default=600, help="Seconds to wait for one answer"
    )
    parser.add_argument(
        "--startup",
        type=float,
        default=240,
        help="Seconds to wait for the server to come up",
    )
    args = parser.parse_args()

    # Progress lines should appear as each answer lands, even through a pipe.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    areas = {area for area in args.area.split(",") if area}
    wanted_ids = {question_id for question_id in args.only.split(",") if question_id}
    questions = [
        item
        for item in load_questions(QUESTIONS)
        if (not areas or item["area"] in areas)
        and (not wanted_ids or item["id"] in wanted_ids)
    ]
    if not questions:
        print("No questions match.", file=sys.stderr)
        return 1

    run_dir = RESULTS / f"{args.label}-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir.mkdir(parents=True)
    base_url = f"http://127.0.0.1:{args.port}"

    # One server per case: a server opens exactly one folder.
    by_patient: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in questions:
        by_patient[item["patient_id"]].append(item)

    rows: list[dict[str, Any]] = []
    for patient_id, items in by_patient.items():
        folder = case_folder(patient_id, Path(args.cases).expanduser())

        if folder is None:
            print(f"{patient_id}: no case folder, skipping {len(items)} questions")
            rows.extend(
                score(item, {"error": "case folder not found"}) for item in items
            )
            continue

        print(f"{patient_id}: starting the server for {len(items)} questions")
        process = start_server(args.launch, folder, args.port, run_dir / "server.log")

        try:
            if not wait_until_healthy(base_url, process, args.startup):
                print(
                    f"{patient_id}: server did not come up; "
                    f"see {run_dir / 'server.log'}"
                )
                rows.extend(
                    score(item, {"error": "server did not start"}) for item in items
                )
                continue

            for item in items:
                row = score(item, ask(base_url, item, folder.name, args.timeout))
                rows.append(row)

                # Written as it goes, so a long run that is stopped keeps its rows.
                with (run_dir / "results.jsonl").open("a") as results:
                    results.write(json.dumps(row) + "\n")

                mark = "pass" if row["passed"] else "FAIL"
                print(f"  {mark}  {row['id']:32s} {row.get('seconds') or 0:6.1f} s")
        finally:
            stop_server(process)

    summary = summarise(rows)

    # Two runs only compare when they ran the same way, so the summary says how.
    summary["settings"] = {
        "launch": args.launch,
        "reasoning_effort": os.getenv("AZURE_OPENAI_REASONING_EFFORT"),
        # Chat completions or the Responses API; tools need the latter on gpt-6.1-sol.
        "dicom_api": os.getenv("DICOM_API", "chat"),
        "deployment": os.getenv("AZURE_OPENAI_DEPLOYMENT") or os.getenv("OPENAI_MODEL"),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print_summary(summary)
    print(f"\nresults: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
