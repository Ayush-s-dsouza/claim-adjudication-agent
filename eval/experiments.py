"""Phase 3: bounded, validation-gated iteration against the frozen baseline.

Rules, enforced here rather than left as prose:
- Every experiment is measured against the VALIDATION split only (never
  tune, never test) -- compared against the baseline's own validation-only
  metrics, not baseline's combined tune+validation numbers.
- Accept a change only if fabrication_rate drops by >= ACCEPT_THRESHOLD
  (2 percentage points) with no more than a small tolerated dip in
  accuracy_legible. Anything smaller is noise, not signal -- reject.
- Every experiment, accepted or not, is appended to experiment_log.jsonl:
  hypothesis, change, before/after metrics, decision, reason. Nothing gets
  silently dropped.
- Hard stop at 6 accepted changes or 12 experiments, whichever comes first
  -- enforced by run_next_experiment() refusing to run past either cap.

Usage (one experiment at a time, by design -- see collect.py --extra-instruction
for how each hypothesis's schema change is expressed):
    python -m eval.experiments run --name h1_nullable_instruction \
        --extra-instruction "If the value is not clearly legible or not present, return null. Do not guess." \
        --hypothesis "Nullable schema + explicit abstention instruction"
    python -m eval.experiments self-consistency --k 3 --n 5
    python -m eval.experiments status
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from eval.collect import run_experiment as collect_run_experiment
from eval.metrics import compute_metrics, is_abstention, is_correct, join_with_manifest, load_results, normalize
from eval.splits import load_split

RESULTS_DIR = Path(__file__).parent / "results"
EXPERIMENT_LOG_PATH = RESULTS_DIR / "experiment_log.jsonl"

ACCEPT_THRESHOLD_POINTS = 2.0  # percentage points; smaller deltas are treated as noise
MAX_ACCEPTED = 6
MAX_EXPERIMENTS = 12


def load_experiment_log() -> list[dict]:
    if not EXPERIMENT_LOG_PATH.exists():
        return []
    with EXPERIMENT_LOG_PATH.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_experiment_log(entry: dict) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with EXPERIMENT_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def validation_only_metrics(experiment: str) -> dict[str, Any]:
    """Metrics for one experiment's results, scoped to the validation split
    only -- this is the number Phase 3 decisions are gated on."""
    results = load_results(experiment)
    rows = join_with_manifest(results)
    validation_rows = [r for r in rows if r.get("case_split") == "validation"]
    return compute_metrics(validation_rows)


def check_budget() -> tuple[int, int]:
    """Returns (experiments_run, accepted_count). Raises if either cap is
    already hit -- called before starting a new experiment, not after."""
    log = load_experiment_log()
    experiments_run = len(log)
    accepted = sum(1 for e in log if e["decision"] == "accept")
    if experiments_run >= MAX_EXPERIMENTS:
        raise RuntimeError(f"Hard stop: {MAX_EXPERIMENTS} experiments already run. No more iteration allowed.")
    if accepted >= MAX_ACCEPTED:
        raise RuntimeError(f"Hard stop: {MAX_ACCEPTED} changes already accepted. No more iteration allowed.")
    return experiments_run, accepted


def decide(baseline: dict, candidate: dict) -> tuple[str, str]:
    """Returns (decision, reason). Accept only if fabrication_rate improves
    by >= ACCEPT_THRESHOLD_POINTS with no material accuracy regression."""
    b_fab = baseline.get("fabrication_rate")
    c_fab = candidate.get("fabrication_rate")
    b_acc = baseline.get("accuracy_legible")
    c_acc = candidate.get("accuracy_legible")

    if b_fab is None or c_fab is None:
        return "reject", "fabrication_rate not computable for baseline or candidate (no illegible/absent rows?)"

    fab_delta_points = (b_fab - c_fab) * 100  # positive = improvement (lower fabrication)
    acc_delta_points = None if (b_acc is None or c_acc is None) else (c_acc - b_acc) * 100

    if fab_delta_points < ACCEPT_THRESHOLD_POINTS:
        return "reject", f"fabrication_rate improved by only {fab_delta_points:.1f} points (< {ACCEPT_THRESHOLD_POINTS} threshold) -- treated as noise"

    if acc_delta_points is not None and acc_delta_points < -ACCEPT_THRESHOLD_POINTS:
        return "reject", f"fabrication_rate improved {fab_delta_points:.1f} points, but accuracy_legible dropped {abs(acc_delta_points):.1f} points -- regression too large"

    return "accept", f"fabrication_rate improved {fab_delta_points:.1f} points, accuracy_legible delta {acc_delta_points:.1f} points -- within tolerance"


def run_next_experiment(name: str, hypothesis: str, extra_instruction: str, n_repeats: int = 5) -> dict:
    """Runs one Phase 3 experiment: validation set only, with the given
    schema-description change, compared against the frozen baseline."""
    experiments_run, accepted = check_budget()

    baseline = validation_only_metrics("baseline")
    validation_cases = load_split("validation")
    out_path = RESULTS_DIR / f"{name}.jsonl"
    collect_run_experiment(validation_cases, name, extra_instruction=extra_instruction, n_repeats=n_repeats, out_path=out_path)
    candidate = validation_only_metrics(name)

    decision, reason = decide(baseline, candidate)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "experiment_number": experiments_run + 1,
        "name": name,
        "hypothesis": hypothesis,
        "change": extra_instruction,
        "baseline_fabrication_rate": baseline.get("fabrication_rate"),
        "candidate_fabrication_rate": candidate.get("fabrication_rate"),
        "baseline_accuracy_legible": baseline.get("accuracy_legible"),
        "candidate_accuracy_legible": candidate.get("accuracy_legible"),
        "decision": decision,
        "reason": reason,
    }
    append_experiment_log(entry)
    return {"baseline": baseline, "candidate": candidate, "decision_entry": entry}


def run_self_consistency_experiment(name: str, k: int, n: int = 5) -> dict:
    """Hypothesis 2: self-consistency, computed from EXISTING baseline data
    -- no new API calls. Re-aggregates baseline's already-collected 5
    repeats per case under a "return value only if >= k of n agree,
    otherwise abstain" rule, then scores that aggregated policy exactly
    like any other experiment."""
    experiments_run, accepted = check_budget()

    baseline_results = load_results("baseline")
    baseline_rows = join_with_manifest(baseline_results)
    validation_rows = [r for r in baseline_rows if r.get("case_split") == "validation"]

    baseline_metrics = compute_metrics(validation_rows)

    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in validation_rows:
        by_case[r["case_id"]].append(r)

    aggregated_rows = []
    for case_id, case_rows in by_case.items():
        case_rows = case_rows[:n]
        values = [normalize(r.get("returned_value")) for r in case_rows]
        non_null = [v for v in values if v]
        vote_counts: dict[str, int] = defaultdict(int)
        for v in non_null:
            vote_counts[v] += 1
        winner, count = (max(vote_counts.items(), key=lambda kv: kv[1]) if vote_counts else (None, 0))
        template = case_rows[0]
        if winner is not None and count >= k:
            agreeing_confidences = [r["confidence"] for r in case_rows if normalize(r.get("returned_value")) == winner and r.get("confidence") is not None]
            aggregated_rows.append(
                {
                    **template,
                    "returned_value": winner,
                    "confidence": sum(agreeing_confidences) / len(agreeing_confidences) if agreeing_confidences else None,
                }
            )
        else:
            aggregated_rows.append({**template, "returned_value": None, "confidence": None})

    candidate_metrics = compute_metrics(aggregated_rows)
    decision, reason = decide(baseline_metrics, candidate_metrics)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "experiment_number": experiments_run + 1,
        "name": name,
        "hypothesis": f"Self-consistency: {k} of {n} repeats must agree, else abstain (computed from existing baseline data, 0 new API calls)",
        "change": f"k={k}, n={n}, reused eval/results/baseline.jsonl",
        "baseline_fabrication_rate": baseline_metrics.get("fabrication_rate"),
        "candidate_fabrication_rate": candidate_metrics.get("fabrication_rate"),
        "baseline_accuracy_legible": baseline_metrics.get("accuracy_legible"),
        "candidate_accuracy_legible": candidate_metrics.get("accuracy_legible"),
        "decision": decision,
        "reason": reason,
    }
    append_experiment_log(entry)
    return {"baseline": baseline_metrics, "candidate": candidate_metrics, "decision_entry": entry}


def print_status() -> None:
    log = load_experiment_log()
    accepted = [e for e in log if e["decision"] == "accept"]
    print(f"Experiments run: {len(log)}/{MAX_EXPERIMENTS}  Accepted: {len(accepted)}/{MAX_ACCEPTED}")
    for e in log:
        print(f"  [{e['decision'].upper()}] {e['name']}: {e['reason']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run")
    p_run.add_argument("--name", required=True)
    p_run.add_argument("--hypothesis", required=True)
    p_run.add_argument("--extra-instruction", required=True)
    p_run.add_argument("--repeats", type=int, default=5)

    p_sc = sub.add_parser("self-consistency")
    p_sc.add_argument("--name", default="h2_self_consistency")
    p_sc.add_argument("--k", type=int, required=True)
    p_sc.add_argument("--n", type=int, default=5)

    sub.add_parser("status")

    args = parser.parse_args()
    if args.cmd == "run":
        result = run_next_experiment(args.name, args.hypothesis, args.extra_instruction, args.repeats)
        print(json.dumps(result["decision_entry"], indent=2))
    elif args.cmd == "self-consistency":
        result = run_self_consistency_experiment(args.name, args.k, args.n)
        print(json.dumps(result["decision_entry"], indent=2))
    elif args.cmd == "status":
        print_status()
