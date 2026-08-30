"""Loads the eval manifest by split. The test split is locked: it must not
be touched during Phase 2 (baseline) or Phase 3 (iteration), only once at
the very end in Phase 4. This module is the single place that enforces
that -- don't read manifest.json directly if you want the lock to mean
anything.

Also home to `assign_splits()`, the general stratified tune/validation/test
assignment used by every case generator (synthetic-English, synthetic-
Devanagari, real-handwriting). It replaces the fixed-grid PATTERN_A/B/C
algebra that used to live in generate_cases.py (solved specifically for a
5x5x4 grid) with a general version that works for any grid shape and any
per-cell repeat count -- the real-handwriting arm's grid is a different
shape (2 field types x 6 degradation levels, uneven repeats), so a
generalized version was needed rather than reused as-is.

`generate_cases.py`'s own PATTERN_A/B/C is left untouched deliberately: it
already produced the committed manifest.json that Phase 2/3's completed,
credit-spent runs are keyed against. Re-deriving that arm's split with a
different (even if equivalent) algorithm risks silently reassigning which
cases are "validation" vs "test" -- not worth the risk for zero benefit.
New arms use `assign_splits()`; the existing English-synthetic arm keeps
its original assignment exactly as generated.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent

VALID_SPLITS = {"tune", "validation", "test"}


class TestSetLockedError(Exception):
    pass


def _manifest_path(arm: Optional[str] = None) -> Path:
    return (BASE_DIR / arm / "manifest.json") if arm else (BASE_DIR / "manifest.json")


def _test_access_log(arm: Optional[str] = None) -> Path:
    return (BASE_DIR / arm / "TEST_SET_ACCESS_LOG.jsonl") if arm else (BASE_DIR / "TEST_SET_ACCESS_LOG.jsonl")


def load_manifest(arm: Optional[str] = None) -> list[dict]:
    return json.loads(_manifest_path(arm).read_text(encoding="utf-8"))


def load_split(split: str, allow_test: bool = False, arm: Optional[str] = None) -> list[dict]:
    """Returns all cases for one split.

    `split="test"` raises TestSetLockedError unless `allow_test=True` is
    passed explicitly (Phase 4 only) or the EVAL_ALLOW_TEST_SET=1
    environment variable is set. Every successful test-set access is
    appended to that arm's TEST_SET_ACCESS_LOG.jsonl so it stays auditable
    -- this is meant to be opened once, not iterated against.

    `arm=None` (default) uses the original English-synthetic arm's paths
    (`eval/manifest.json`, `eval/results/`) for backward compatibility.
    Pass e.g. `arm="real_handwriting"` to use `eval/real_handwriting/...`.
    """
    if split not in VALID_SPLITS:
        raise ValueError(f"split must be one of {VALID_SPLITS}, got {split!r}")

    if split == "test":
        env_override = os.environ.get("EVAL_ALLOW_TEST_SET") == "1"
        if not (allow_test or env_override):
            raise TestSetLockedError(
                "The test split is locked during iteration (Phase 1-3). "
                "It may only be opened once, in Phase 4, by calling "
                "load_split('test', allow_test=True)."
            )
        log_path = _test_access_log(arm)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {"timestamp": datetime.now(timezone.utc).isoformat(), "arm": arm, "via": "env" if env_override else "explicit"}
                )
                + "\n"
            )

    return [case for case in load_manifest(arm) if case["split"] == split]


def assign_splits(
    cases: list[dict],
    group_keys: tuple[str, ...] = ("field_type", "degradation_level"),
    value_key: str = "value_index",
    fracs: tuple[float, float, float] = (0.4, 0.3, 0.3),
    splits: tuple[str, str, str] = ("tune", "validation", "test"),
) -> list[dict]:
    """General stratified split assignment for any grid shape / repeat
    count. Mutates each case dict in place (adds "split"), and returns
    `cases`.

    Builds one repeating assignment cycle from `fracs` (e.g. (0.4,0.3,0.3)
    -> a 10-slot cycle: 4 tune, 3 validation, 3 test, interleaved as evenly
    as possible via largest-remainder allocation) and walks it per case at
    position `(value_index + group_index) % len(cycle)`. Rotating the
    starting phase by each group's index means no single field-type/
    degradation-level combination is systematically stuck with the same
    slice of the cycle -- important because with only a handful of repeats
    per cell, a fixed (non-rotating) cycle would silently bias whichever
    cells happen to align with its start.

    `fracs` must currently resolve to an exact-integer cycle (denominator
    10 for (0.4, 0.3, 0.3)); this isn't generalized further because no
    other ratio is used anywhere in this project.
    """
    denom = 10
    target_counts = [round(f * denom) for f in fracs]
    if sum(target_counts) != denom:
        raise ValueError(f"fracs {fracs} must resolve to integer counts summing to {denom}")

    # Largest-remainder-style interleaving: repeatedly pick whichever split
    # is furthest behind its target share, so the cycle spreads splits out
    # evenly (e.g. T,V,S,T,V,S,T,V,S,T) rather than clumping (T,T,T,T,V,V,V,S,S,S).
    cycle: list[str] = []
    remaining = list(target_counts)
    progress = [0.0] * len(fracs)
    for _ in range(denom):
        progress = [p + f for p, f in zip(progress, fracs)]
        idx = max((i for i in range(len(fracs)) if remaining[i] > 0), key=lambda i: progress[i])
        cycle.append(splits[idx])
        progress[idx] -= 1.0
        remaining[idx] -= 1

    groups = sorted({tuple(case[k] for k in group_keys) for case in cases})
    group_index = {group: i for i, group in enumerate(groups)}

    for case in cases:
        group = tuple(case[k] for k in group_keys)
        position = (case[value_key] + group_index[group]) % denom
        case["split"] = cycle[position]

    return cases
