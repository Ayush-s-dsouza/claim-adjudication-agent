"""Computes the Phase 2 metrics from a raw results log + the ground-truth
manifest. Deliberately does NOT assume confidence is 0 on an abstention --
the first live smoke test for this eval set already showed Sarvam can
return a null value alongside confidence 0.997, so treating "abstained" and
"low confidence" as the same thing would hide exactly the phenomenon this
diagnosis is trying to measure.

Usage:
    python -m eval.metrics --experiment baseline
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from eval.splits import load_manifest

RESULTS_DIR = Path(__file__).parent / "results"

CONFIDENCE_THRESHOLD = 0.80  # matches sufficiency.py's CONFIDENCE_THRESHOLD
CALIBRATION_BUCKETS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.001)]

LEGIBLE_LEVELS = {"clean", "mild", "heavy"}
# The real-handwriting arm splits "illegible" into two sub-tiers
# (illegible_processed / illegible_natural, see eval/generate_real_handwriting.py)
# that the synthetic arms never produce. Both are "illegible-like" for
# correctness/fabrication purposes -- correct behavior is still abstain-or-
# match-truth -- but compute_metrics() reports them separately too (see
# abstention_rate_by_level) rather than silently merging them, since a
# post-processed-illegible sample and a naturally-bad-handwriting sample
# are not the same claim about the model.
ILLEGIBLE_LIKE_LEVELS = {"illegible", "illegible_processed", "illegible_natural"}
ABSTAIN_EXPECTED_LEVELS = ILLEGIBLE_LIKE_LEVELS | {"absent"}


def normalize(value: Optional[str]) -> Optional[str]:
    """Whitespace-collapse plus Unicode NFC normalization. The latter
    matters specifically for Devanagari: ground-truth labels and Sarvam's
    responses can represent the same visible glyph with different
    codepoint sequences (e.g. precomposed U+095E "फ़" vs base consonant +
    combining nukta U+092B U+093C) -- confirmed directly against a real
    API response, not assumed. Without NFC normalization here, visually
    and semantically identical strings compare unequal and get scored as
    wrong, which would silently inflate every Devanagari-arm error rate."""
    if value is None:
        return None
    return unicodedata.normalize("NFC", " ".join(str(value).strip().split()))


def load_results(experiment: str, results_dir: Optional[Path] = None) -> list[dict]:
    path = (results_dir or RESULTS_DIR) / f"{experiment}.jsonl"
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def join_with_manifest(results: list[dict], arm: Optional[str] = None) -> list[dict]:
    manifest_by_id = {c["case_id"]: c for c in load_manifest(arm)}
    joined = []
    for r in results:
        case = manifest_by_id.get(r["case_id"])
        if case is None:
            continue
        row = {**r, **{f"case_{k}": v for k, v in case.items() if k != "case_id"}}
        joined.append(row)
    return joined


def is_abstention(row: dict) -> bool:
    value = normalize(row.get("returned_value"))
    return value is None or value == ""


def is_correct(row: dict) -> bool:
    """Uniform correctness definition across all degradation levels:
    - legible (clean/mild/heavy): correct iff returned value matches truth.
    - illegible (or its illegible_processed/illegible_natural sub-tiers):
      correct iff abstained, OR returned value happens to match the
      (known, but human/model-illegible) true value.
    - absent: correct iff abstained -- any returned value is wrong, there
      was nothing there to read.
    """
    level = row["case_degradation_level"]
    truth = normalize(row.get("case_true_value"))
    returned = normalize(row.get("returned_value"))

    if level == "absent":
        return is_abstention(row)
    if level in ILLEGIBLE_LIKE_LEVELS:
        return is_abstention(row) or returned == truth
    return returned == truth  # clean / mild / heavy


def is_fabrication(row: dict) -> bool:
    """A confident wrong value on illegible/absent input -- the number the
    whole diagnosis is trying to drive to zero. Requires BOTH a non-null
    answer that's wrong AND confidence >= CONFIDENCE_THRESHOLD, matching
    sufficiency.py's actual gate: a wrong answer below threshold would
    already get flagged for human review by the existing demo pipeline, so
    it's not the dangerous case. A wrong answer at high confidence sails
    straight through."""
    level = row["case_degradation_level"]
    if level not in ABSTAIN_EXPECTED_LEVELS:
        return False
    if is_abstention(row):
        return False
    confidence = row.get("confidence")
    if confidence is None:
        return False
    return not is_correct(row) and confidence >= CONFIDENCE_THRESHOLD


def compute_metrics(rows: list[dict]) -> dict[str, Any]:
    n = len(rows)
    errored = [r for r in rows if r.get("error")]
    usable = [r for r in rows if not r.get("error")]

    illegible_rows = [r for r in usable if r["case_degradation_level"] in ILLEGIBLE_LIKE_LEVELS]
    absent_rows = [r for r in usable if r["case_degradation_level"] == "absent"]
    legible_rows = [r for r in usable if r["case_degradation_level"] in LEGIBLE_LEVELS]

    fabrications = [r for r in usable if is_fabrication(r)]
    abstain_illegible = [r for r in illegible_rows if is_abstention(r)]
    abstain_absent = [r for r in absent_rows if is_abstention(r)]
    legible_correct = [r for r in legible_rows if is_correct(r)]

    # Per-level breakdown (abstention rate, count). On the synthetic arms
    # this just re-states illegible/absent; on the real-handwriting arm it
    # separates illegible_processed from illegible_natural rather than
    # merging them into one number that would imply false comparability
    # between "degraded by us" and "genuinely bad handwriting."
    abstention_rate_by_level: dict[str, Optional[float]] = {}
    levels_present = sorted({r["case_degradation_level"] for r in usable})
    for level in levels_present:
        level_rows = [r for r in usable if r["case_degradation_level"] == level]
        abstention_rate_by_level[level] = (
            sum(is_abstention(r) for r in level_rows) / len(level_rows) if level_rows else None
        )

    # Confidence/abstention decoupling: how often does an abstained
    # response still carry a non-trivial confidence value?
    abstentions_with_confidence = [
        r for r in usable if is_abstention(r) and r.get("confidence") is not None
    ]
    high_conf_abstentions = [
        r for r in abstentions_with_confidence if r["confidence"] >= CONFIDENCE_THRESHOLD
    ]

    # Calibration: bucket every usable row by its own confidence, report
    # the correctness rate within each bucket.
    calibration = []
    for lo, hi in CALIBRATION_BUCKETS:
        bucket = [r for r in usable if r.get("confidence") is not None and lo <= r["confidence"] < hi]
        if bucket:
            acc = sum(is_correct(r) for r in bucket) / len(bucket)
        else:
            acc = None
        calibration.append({"range": f"[{lo:.1f}, {hi:.1f})", "n": len(bucket), "accuracy": acc})

    # Run-to-run variance: for each case_id, do all repeats agree on
    # abstain-vs-value and, if valued, on the exact value?
    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in usable:
        by_case[r["case_id"]].append(r)
    non_unanimous = 0
    for case_id, case_rows in by_case.items():
        outcomes = {(is_abstention(r), normalize(r.get("returned_value"))) for r in case_rows}
        if len(outcomes) > 1:
            non_unanimous += 1

    return {
        "total_rows": n,
        "errored_rows": len(errored),
        "usable_rows": len(usable),
        "fabrication_rate": len(fabrications) / len(illegible_rows + absent_rows) if (illegible_rows or absent_rows) else None,
        "fabrication_count": len(fabrications),
        "fabrication_denominator": len(illegible_rows) + len(absent_rows),
        "abstention_rate_illegible": len(abstain_illegible) / len(illegible_rows) if illegible_rows else None,
        "abstention_rate_absent": len(abstain_absent) / len(absent_rows) if absent_rows else None,
        "accuracy_legible": len(legible_correct) / len(legible_rows) if legible_rows else None,
        "high_confidence_abstention_rate": (
            len(high_conf_abstentions) / len(abstentions_with_confidence) if abstentions_with_confidence else None
        ),
        "high_confidence_abstention_count": len(high_conf_abstentions),
        "total_abstentions_with_confidence": len(abstentions_with_confidence),
        "calibration": calibration,
        "abstention_rate_by_level": abstention_rate_by_level,
        "cases_total": len(by_case),
        "cases_non_unanimous_across_repeats": non_unanimous,
        "non_unanimous_rate": non_unanimous / len(by_case) if by_case else None,
    }


def print_report(metrics: dict[str, Any]) -> None:
    print(f"Total rows: {metrics['total_rows']}  (usable: {metrics['usable_rows']}, errored: {metrics['errored_rows']})")
    print()
    print(f"Fabrication rate (confident wrong on illegible/absent): "
          f"{metrics['fabrication_rate']:.1%} ({metrics['fabrication_count']}/{metrics['fabrication_denominator']})"
          if metrics["fabrication_rate"] is not None else "Fabrication rate: n/a")
    print(f"Abstention rate on illegible input: {metrics['abstention_rate_illegible']:.1%}"
          if metrics["abstention_rate_illegible"] is not None else "Abstention rate (illegible): n/a")
    print(f"Abstention rate on absent input: {metrics['abstention_rate_absent']:.1%}"
          if metrics["abstention_rate_absent"] is not None else "Abstention rate (absent): n/a")
    print(f"Extraction accuracy on legible input: {metrics['accuracy_legible']:.1%}"
          if metrics["accuracy_legible"] is not None else "Accuracy (legible): n/a")
    by_level = metrics.get("abstention_rate_by_level") or {}
    extra_levels = [lvl for lvl in by_level if lvl not in ("illegible", "absent") and lvl in LEGIBLE_LEVELS.union(ILLEGIBLE_LIKE_LEVELS)]
    if extra_levels:
        print("Abstention rate by level (sub-tiers, not merged):")
        for lvl in sorted(by_level):
            rate = by_level[lvl]
            print(f"  {lvl}: {rate:.1%}" if rate is not None else f"  {lvl}: n/a")
    print()
    print(f"Of {metrics['total_abstentions_with_confidence']} abstentions with a confidence value, "
          f"{metrics['high_confidence_abstention_count']} ({metrics['high_confidence_abstention_rate']:.1%}) "
          f"were reported at confidence >= {CONFIDENCE_THRESHOLD} despite returning no value."
          if metrics["high_confidence_abstention_rate"] is not None else "High-confidence-abstention rate: n/a")
    print()
    print("Calibration (confidence bucket -> accuracy):")
    for bucket in metrics["calibration"]:
        acc_str = f"{bucket['accuracy']:.1%}" if bucket["accuracy"] is not None else "n/a"
        print(f"  {bucket['range']}: n={bucket['n']:>3}  accuracy={acc_str}")
    print()
    print(f"Cases with non-unanimous outcomes across repeats: "
          f"{metrics['cases_non_unanimous_across_repeats']}/{metrics['cases_total']} "
          f"({metrics['non_unanimous_rate']:.1%})" if metrics["non_unanimous_rate"] is not None else "n/a")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--arm", default=None, help="e.g. 'real_handwriting'; omit for the original English-synthetic arm")
    args = parser.parse_args()

    results_dir = (Path(__file__).parent / args.arm / "results") if args.arm else None
    results = load_results(args.experiment, results_dir=results_dir)
    rows = join_with_manifest(results, arm=args.arm)
    metrics = compute_metrics(rows)
    print_report(metrics)
