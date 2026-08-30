"""Loads the eval manifest by split. The test split is locked: it must not
be touched during Phase 2 (baseline) or Phase 3 (iteration), only once at
the very end in Phase 4. This module is the single place that enforces
that -- don't read manifest.json directly if you want the lock to mean
anything.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_PATH = Path(__file__).parent / "manifest.json"
TEST_ACCESS_LOG = Path(__file__).parent / "TEST_SET_ACCESS_LOG.jsonl"

VALID_SPLITS = {"tune", "validation", "test"}


class TestSetLockedError(Exception):
    pass


def load_manifest() -> list[dict]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def load_split(split: str, allow_test: bool = False) -> list[dict]:
    """Returns all cases for one split.

    `split="test"` raises TestSetLockedError unless `allow_test=True` is
    passed explicitly (Phase 4 only) or the EVAL_ALLOW_TEST_SET=1
    environment variable is set. Every successful test-set access is
    appended to TEST_SET_ACCESS_LOG.jsonl so it stays auditable -- this is
    meant to be opened once, not iterated against.
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
        with TEST_ACCESS_LOG.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "via": "env" if env_override else "explicit"})
                + "\n"
            )

    return [case for case in load_manifest() if case["split"] == split]
