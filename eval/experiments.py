"""Phase 3: bounded, validation-gated iteration against the frozen baseline.

Rules, enforced here rather than left as prose:
- Every experiment is measured against the VALIDATION split only (never
  tune, never test) -- compared against the baseline's own validation-only
  metrics, not baseline's combined tune+validation numbers.
- Accept a change only if decide() confirms both a benefit and the absence
  of a confirmed cost, using a case-level paired cluster bootstrap against
  the actual rows, not a flat round-number threshold. See decide()'s own
  docstring below for the exact rule and why it replaced the original flat
  2-point threshold, which turned out to sit far below this eval's minimum
  detectable effect at the sample sizes actually collected.
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
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from eval.collect import run_experiment as collect_run_experiment
from eval.metrics import (
    ABSTAIN_EXPECTED_LEVELS,
    LEGIBLE_LEVELS,
    compute_metrics,
    is_abstention,
    is_correct,
    is_fabrication,
    join_with_manifest,
    load_results,
    normalize,
)
from eval.splits import load_split

BASE_DIR = Path(__file__).parent

MAX_ACCEPTED = 6
MAX_EXPERIMENTS = 12

CI_CONFIDENCE = 0.90     # two-sided bootstrap confidence level decide() tests both benefit and cost at
N_BOOTSTRAP = 10000      # bootstrap resamples per delta
BOOTSTRAP_SEED = 1337    # fixed, so re-running decide() on the same rows reproduces the same verdict


def _results_dir(arm: Optional[str] = None) -> Path:
    return (BASE_DIR / arm / "results") if arm else (BASE_DIR / "results")


def _experiment_log_path(arm: Optional[str] = None) -> Path:
    return _results_dir(arm) / "experiment_log.jsonl"


def load_experiment_log(arm: Optional[str] = None) -> list[dict]:
    path = _experiment_log_path(arm)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_experiment_log(entry: dict, arm: Optional[str] = None) -> None:
    _results_dir(arm).mkdir(parents=True, exist_ok=True)
    with _experiment_log_path(arm).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def validation_only_rows(experiment: str, arm: Optional[str] = None) -> list[dict]:
    """Raw joined per-call rows for one experiment, scoped to the validation
    split -- the actual data decide()'s bootstrap resamples. Point estimates
    (validation_only_metrics, below) are a summary of this, not a
    replacement for it -- decide() needs the rows themselves to resample."""
    results = load_results(experiment, results_dir=_results_dir(arm))
    rows = join_with_manifest(results, arm=arm)
    return [r for r in rows if r.get("case_split") == "validation"]


def validation_only_metrics(experiment: str, arm: Optional[str] = None) -> dict[str, Any]:
    """Metrics for one experiment's results, scoped to the validation split
    only -- this is the point estimate Phase 3 reports alongside decide()'s
    bootstrap-based verdict."""
    return compute_metrics(validation_only_rows(experiment, arm))


def check_budget(arm: Optional[str] = None) -> tuple[int, int]:
    """Returns (experiments_run, accepted_count). Raises if either cap is
    already hit -- called before starting a new experiment, not after."""
    log = load_experiment_log(arm)
    experiments_run = len(log)
    accepted = sum(1 for e in log if e["decision"] == "accept")
    if experiments_run >= MAX_EXPERIMENTS:
        raise RuntimeError(f"Hard stop: {MAX_EXPERIMENTS} experiments already run. No more iteration allowed.")
    if accepted >= MAX_ACCEPTED:
        raise RuntimeError(f"Hard stop: {MAX_ACCEPTED} changes already accepted. No more iteration allowed.")
    return experiments_run, accepted


def fab_rate(rows: list[dict]) -> Optional[float]:
    return sum(is_fabrication(r) for r in rows) / len(rows) if rows else None


def acc_rate(rows: list[dict]) -> Optional[float]:
    return sum(is_correct(r) for r in rows) / len(rows) if rows else None


def cluster_bootstrap_deltas(
    baseline_rows: list[dict], candidate_rows: list[dict], case_ids: list[str], rate_fn, n_boot: int = N_BOOTSTRAP, seed: int = BOOTSTRAP_SEED
) -> list[float]:
    """Case-level cluster bootstrap for the paired difference between two
    conditions evaluated on the same cases -- baseline vs. a candidate
    schema change, or baseline vs. its self-consistency aggregation.

    Resamples case_ids with replacement, not individual rows: repeated
    calls on the same case are correlated (same handwriting, same likely
    failure mode), not independent draws, so the case is the unit that
    gets resampled, not the row. Each resampled case contributes whichever
    rows it actually has on each side -- 5 repeats for a fresh experiment,
    1 aggregated row for a self-consistency candidate -- rate_fn just
    averages over whatever bag results, so both shapes work unmodified.

    Returns the list of bootstrap deltas (baseline_rate - candidate_rate).
    For a fabrication rate, positive means the candidate fabricated less
    than baseline (a benefit). For an accuracy rate, positive means the
    candidate scored lower than baseline (a cost).
    """
    baseline_by_case: dict[str, list[dict]] = defaultdict(list)
    for r in baseline_rows:
        baseline_by_case[r["case_id"]].append(r)
    candidate_by_case: dict[str, list[dict]] = defaultdict(list)
    for r in candidate_rows:
        candidate_by_case[r["case_id"]].append(r)

    rng = random.Random(seed)
    deltas = []
    for _ in range(n_boot):
        sample = rng.choices(case_ids, k=len(case_ids))
        base_bag, cand_bag = [], []
        for cid in sample:
            base_bag.extend(baseline_by_case[cid])
            cand_bag.extend(candidate_by_case[cid])
        deltas.append(rate_fn(base_bag) - rate_fn(cand_bag))
    return deltas


def bootstrap_ci(deltas: list[float], confidence: float = CI_CONFIDENCE) -> tuple[float, float]:
    """Percentile confidence interval from a bootstrap delta distribution."""
    tail = (1 - confidence) / 2
    s = sorted(deltas)
    n = len(s)
    lo = s[int(tail * n)]
    hi = s[min(int((1 - tail) * n), n - 1)]
    return lo, hi


def decide(baseline_rows: list[dict], candidate_rows: list[dict]) -> tuple[str, str]:
    """Statistically grounded replacement for the original flat 2-point
    threshold rule.

    The original rule accepted a change if its point-estimate fabrication
    rate improved by >= 2 percentage points, with no more than a 2-point
    accuracy_legible regression. That threshold was a round number chosen
    before any experiment ran, not derived from this eval's actual sample
    size. A power calculation against the real-handwriting arm's
    validation split (12 fabrication-eligible cases, 15 legible cases)
    showed the minimum detectable effect at 90% confidence was roughly an
    order of magnitude larger than 2 points, so the old rule could not
    reliably have told a real improvement from noise -- it happened to
    reject one hypothesis (H1) whose benefit was in fact real and confirm
    another (H2) whose benefit was not, which only came to light once this
    was checked properly. See the README's "Acceptance rule" section for
    the full derivation.

    This version runs a case-level paired cluster bootstrap (see
    cluster_bootstrap_deltas above -- the same resampling method already
    used elsewhere in this repo's confidence diagnosis) directly on the
    rows, and asks two separate, independently-tested questions instead of
    comparing one pair of point estimates against a round number:

    - BENEFIT CONFIRMED: the fabrication-rate improvement's CI_CONFIDENCE
      (90%) bootstrap confidence interval excludes zero, i.e. its lower
      bound is > 0.
    - COST CONFIRMED: the accuracy-legible regression's confidence
      interval, at the same confidence level, also excludes zero, i.e. its
      lower bound is > 0 -- a real accuracy drop, not one that could
      plausibly be noise.

    Decision:
    - No confirmed benefit -> reject. A candidate has to clear its own
      statistical bar before a cost is even worth discussing.
    - Confirmed benefit, no confirmed cost -> accept.
    - Confirmed benefit AND confirmed cost -> accept only if the benefit's
      worst case (its CI lower bound) still exceeds the cost's worst case
      (its CI upper bound). A real benefit that might be smaller than a
      real cost is not a trade this function will wave through.

    Both bootstraps run at N_BOOTSTRAP (10,000) resamples with a fixed
    BOOTSTRAP_SEED, so re-running this function against the same rows
    reproduces the same verdict -- an auditor can re-run it rather than
    take the result on faith.
    """
    fab_case_ids = sorted({r["case_id"] for r in baseline_rows if r["case_degradation_level"] in ABSTAIN_EXPECTED_LEVELS})
    legible_case_ids = sorted({r["case_id"] for r in baseline_rows if r["case_degradation_level"] in LEGIBLE_LEVELS})

    if not fab_case_ids:
        return "reject", "no illegible/absent cases in this data, fabrication-rate benefit is not computable"

    fab_deltas = cluster_bootstrap_deltas(baseline_rows, candidate_rows, fab_case_ids, fab_rate)
    benefit_lo, benefit_hi = bootstrap_ci(fab_deltas)
    benefit_confirmed = benefit_lo > 0

    if not benefit_confirmed:
        return "reject", (
            f"fabrication-rate improvement {benefit_lo*100:+.1f} to {benefit_hi*100:+.1f} points at "
            f"{CI_CONFIDENCE:.0%} confidence, interval includes zero, benefit not confirmed"
        )

    if not legible_case_ids:
        return "accept", (
            f"fabrication-rate improvement confirmed ({benefit_lo*100:+.1f} to {benefit_hi*100:+.1f} points), "
            f"no legible cases available to test for a cost"
        )

    acc_deltas = cluster_bootstrap_deltas(baseline_rows, candidate_rows, legible_case_ids, acc_rate)
    cost_lo, cost_hi = bootstrap_ci(acc_deltas)
    cost_confirmed = cost_lo > 0

    if not cost_confirmed:
        return "accept", (
            f"fabrication-rate improvement confirmed ({benefit_lo*100:+.1f} to {benefit_hi*100:+.1f} points), "
            f"accuracy-legible cost {cost_lo*100:+.1f} to {cost_hi*100:+.1f} points at {CI_CONFIDENCE:.0%} confidence, "
            f"interval includes zero, cost not confirmed"
        )

    if benefit_lo > cost_hi:
        return "accept", (
            f"benefit confirmed (lower bound {benefit_lo*100:+.1f} points) and cost confirmed "
            f"(upper bound {cost_hi*100:+.1f} points), benefit exceeds cost even in the worst case"
        )
    return "reject", (
        f"benefit confirmed (lower bound {benefit_lo*100:+.1f} points) and cost confirmed "
        f"(upper bound {cost_hi*100:+.1f} points), cost is not safely smaller than benefit"
    )


def run_next_experiment(
    name: str, hypothesis: str, extra_instruction: str, n_repeats: int = 5, arm: Optional[str] = None, language: str = "en-IN"
) -> dict:
    """Runs one Phase 3 experiment: validation set only, with the given
    schema-description change, compared against the frozen baseline."""
    experiments_run, accepted = check_budget(arm)

    baseline_rows = validation_only_rows("baseline", arm)
    baseline = compute_metrics(baseline_rows)
    validation_cases = load_split("validation", arm=arm)
    out_path = _results_dir(arm) / f"{name}.jsonl"
    collect_run_experiment(validation_cases, name, extra_instruction=extra_instruction, n_repeats=n_repeats, out_path=out_path, arm=arm, language=language)
    candidate_rows = validation_only_rows(name, arm)
    candidate = compute_metrics(candidate_rows)

    decision, reason = decide(baseline_rows, candidate_rows)
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
    append_experiment_log(entry, arm)
    return {"baseline": baseline, "candidate": candidate, "decision_entry": entry}


def apply_self_consistency(rows: list[dict], k: int, n: int) -> list[dict]:
    """Re-aggregates repeated-call rows under a "return value only if >= k
    of n agree, otherwise abstain" rule. Pure function, no side effects --
    shared by Phase 3's gated experiment below and Phase 4's final report,
    which must NOT touch decide() or the experiment log (it's a one-time
    report, not another gated decision)."""
    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
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
    return aggregated_rows


def run_self_consistency_experiment(name: str, k: int, n: int = 5, arm: Optional[str] = None) -> dict:
    """Hypothesis 2: self-consistency, computed from EXISTING baseline data
    -- no new API calls. Re-aggregates baseline's already-collected 5
    repeats per case under a "return value only if >= k of n agree,
    otherwise abstain" rule, then scores that aggregated policy exactly
    like any other experiment."""
    experiments_run, accepted = check_budget(arm)

    validation_rows = validation_only_rows("baseline", arm)
    baseline_metrics = compute_metrics(validation_rows)
    aggregated_rows = apply_self_consistency(validation_rows, k, n)
    candidate_metrics = compute_metrics(aggregated_rows)
    decision, reason = decide(validation_rows, aggregated_rows)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "experiment_number": experiments_run + 1,
        "name": name,
        "hypothesis": f"Self-consistency: {k} of {n} repeats must agree, else abstain (computed from existing baseline data, 0 new API calls)",
        "change": f"k={k}, n={n}, reused {_results_dir(arm) / 'baseline.jsonl'}",
        "baseline_fabrication_rate": baseline_metrics.get("fabrication_rate"),
        "candidate_fabrication_rate": candidate_metrics.get("fabrication_rate"),
        "baseline_accuracy_legible": baseline_metrics.get("accuracy_legible"),
        "candidate_accuracy_legible": candidate_metrics.get("accuracy_legible"),
        "decision": decision,
        "reason": reason,
    }
    append_experiment_log(entry, arm)
    return {"baseline": baseline_metrics, "candidate": candidate_metrics, "decision_entry": entry}


def print_status(arm: Optional[str] = None) -> None:
    log = load_experiment_log(arm)
    accepted = [e for e in log if e["decision"] == "accept"]
    print(f"Experiments run: {len(log)}/{MAX_EXPERIMENTS}  Accepted: {len(accepted)}/{MAX_ACCEPTED}")
    for e in log:
        print(f"  [{e['decision'].upper()}] {e['name']}: {e['reason']}")


NON_ENGLISH_ARMS = {"synth_devanagari", "real_handwriting"}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", default=None, help="e.g. 'real_handwriting'; omit for the original English-synthetic arm")
    parser.add_argument("--language", default="en-IN", help="BCP-47 code, e.g. hi-IN for the Devanagari arms")
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
    if args.cmd == "run" and args.arm in NON_ENGLISH_ARMS and args.language == "en-IN":
        parser.error(f"--arm {args.arm} requires --language to be passed explicitly (e.g. hi-IN), not left at the en-IN default")

    if args.cmd == "run":
        result = run_next_experiment(args.name, args.hypothesis, args.extra_instruction, args.repeats, arm=args.arm, language=args.language)
        print(json.dumps(result["decision_entry"], indent=2))
    elif args.cmd == "self-consistency":
        result = run_self_consistency_experiment(args.name, args.k, args.n, arm=args.arm)
        print(json.dumps(result["decision_entry"], indent=2))
    elif args.cmd == "status":
        print_status(arm=args.arm)
