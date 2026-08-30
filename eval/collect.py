"""Runs eval cases against the real Sarvam Extract API and logs raw
results. Deliberately bypasses extract.py's SarvamExtractor: that wrapper
already normalizes "not found" to confidence=0.0 regardless of what Sarvam
actually sent, which would hide exactly the signal this diagnosis needs.
Here we log Sarvam's raw annotation for the field, unmodified.

Results are appended to a JSONL file per experiment, one row per
(case, run). The log is resumable -- rerunning skips (case_id, run_index)
pairs already present, so a multi-hour run can be interrupted and
continued without repeating work or losing progress.

Usage:
    python -m eval.collect --experiment baseline --split tune validation --repeats 5
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from sarvamai import SarvamAI

from eval.splits import load_split

RESULTS_DIR = Path(__file__).parent / "results"

# Base extraction instruction per field type. Phase 3 experiments vary this
# by passing `extra_instruction`, appended to the base description.
BASE_DESCRIPTIONS: dict[str, str] = {
    "typeset": "The printed text value written next to the 'Field:' label.",
    "name": "The person's name written next to the 'Field:' label.",
    "numeric": "The number written next to the 'Field:' label.",
    "date": "The date written next to the 'Field:' label, in DD-MM-YYYY format.",
    "handwriting": "The handwritten value written next to the 'Field:' label.",
}


def build_schema(field_type: str, extra_instruction: str = "") -> dict:
    description = BASE_DESCRIPTIONS[field_type]
    if extra_instruction:
        description = f"{description} {extra_instruction}"
    return {
        "type": "object",
        "properties": {"value": {"type": "string", "description": description}},
    }


class RateLimiter:
    """Keeps requests under Sarvam's flat 10/minute Document Intelligence
    cap, using a sliding 60s window over every HTTP call (submit, each
    status poll, and the results fetch all count)."""

    def __init__(self, max_per_minute: int = 10):
        self.max_per_minute = max_per_minute
        self.timestamps: deque[float] = deque()

    def wait(self) -> None:
        now = time.monotonic()
        while self.timestamps and now - self.timestamps[0] > 60:
            self.timestamps.popleft()
        if len(self.timestamps) >= self.max_per_minute:
            sleep_for = 60 - (now - self.timestamps[0]) + 0.25
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            while self.timestamps and now - self.timestamps[0] > 60:
                self.timestamps.popleft()
        self.timestamps.append(now)


TERMINAL_STATUSES = {"completed", "partially_completed", "failed", "rejected"}


def run_case_once(
    client: SarvamAI,
    limiter: RateLimiter,
    case: dict,
    run_index: int,
    experiment: str,
    extra_instruction: str = "",
    language: str = "en-IN",
    poll_interval: float = 3.0,
    poll_timeout: float = 120.0,
) -> dict:
    record: dict[str, Any] = {
        "experiment": experiment,
        "case_id": case["case_id"],
        "run_index": run_index,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    image_path = Path(__file__).parent / case["image_path"]
    schema = build_schema(case["field_type"], extra_instruction)

    try:
        limiter.wait()
        with image_path.open("rb") as f:
            job = client.doc_ai.extract(
                file=[(image_path.name, f, "image/png")],
                schema=json.dumps(schema),
                language=language,
                output_format="json",
            )
        record["job_id"] = job.job_id

        elapsed = 0.0
        status = None
        while True:
            limiter.wait()
            status = client.doc_ai.get_status(job_id=job.job_id)
            if status.status in TERMINAL_STATUSES:
                break
            if elapsed >= poll_timeout:
                raise TimeoutError(f"job {job.job_id} did not reach terminal status within {poll_timeout}s")
            time.sleep(poll_interval)
            elapsed += poll_interval
        record["status"] = status.status

        if status.status not in ("completed", "partially_completed"):
            record["returned_value"] = None
            record["confidence"] = None
            record["raw_annotation"] = None
            record["error"] = f"non-success terminal status: {status.status}"
            return record

        limiter.wait()
        results = client.doc_ai.get_results(job_id=job.job_id)
        result: dict = getattr(results, "result", None) or {}
        annotations: dict = getattr(results, "annotations", None) or {}

        record["returned_value"] = result.get("value")
        annotation = annotations.get("value") if isinstance(annotations, dict) else None
        record["raw_annotation"] = annotation
        record["confidence"] = annotation.get("confidence") if isinstance(annotation, dict) else None
        record["error"] = None
        return record

    except Exception as e:  # noqa: BLE001 -- this is a data-collection loop, log and continue
        record.setdefault("status", "exception")
        record["returned_value"] = None
        record["confidence"] = None
        record["raw_annotation"] = None
        record["error"] = f"{type(e).__name__}: {e}"
        return record


def load_already_done(out_path: Path) -> set[tuple[str, int]]:
    if not out_path.exists():
        return set()
    done = set()
    with out_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            done.add((row["case_id"], row["run_index"]))
    return done


def run_experiment(
    cases: list[dict],
    experiment: str,
    extra_instruction: str = "",
    n_repeats: int = 5,
    out_path: Optional[Path] = None,
) -> Path:
    load_dotenv()
    client = SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"])
    limiter = RateLimiter(max_per_minute=10)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = out_path or (RESULTS_DIR / f"{experiment}.jsonl")
    already_done = load_already_done(out_path)

    total = len(cases) * n_repeats
    done_count = 0
    with out_path.open("a", encoding="utf-8") as f:
        for case in cases:
            for run_index in range(n_repeats):
                done_count += 1
                if (case["case_id"], run_index) in already_done:
                    continue
                record = run_case_once(client, limiter, case, run_index, experiment, extra_instruction)
                f.write(json.dumps(record) + "\n")
                f.flush()
                print(
                    f"[{done_count}/{total}] {case['case_id']} run={run_index} "
                    f"status={record.get('status')} value={record.get('returned_value')!r} "
                    f"confidence={record.get('confidence')}"
                )
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--split", nargs="+", default=["tune", "validation"])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--extra-instruction", default="")
    args = parser.parse_args()

    all_cases = []
    for split in args.split:
        all_cases.extend(load_split(split))

    out_path = run_experiment(all_cases, args.experiment, args.extra_instruction, args.repeats)
    print(f"done -- results in {out_path}")
