"""Append-only audit log for a claim.

Every extraction, confidence score, and sufficiency verdict is written as
one JSON line and never rewritten. `replay()` reads them back in order, so
a claim's full processing history can be reconstructed after the fact --
this is what lets a regulator or auditor check what happened without
re-running anything.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from schema import ExtractedField, SufficiencyReport

DEFAULT_LOG_DIR = Path("audit_logs")


class AuditLog:
    def __init__(self, claim_id: str, log_dir: Path = DEFAULT_LOG_DIR):
        self.claim_id = claim_id
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / f"{claim_id}.jsonl"

    def _append(self, event_type: str, detail: dict[str, Any]) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "claim_id": self.claim_id,
            "event_type": event_type,
            "detail": detail,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def log_field_extracted(self, document_type: str, filename: str, field: ExtractedField) -> None:
        self._append(
            "field_extracted",
            {
                "document_type": document_type,
                "filename": filename,
                "field_name": field.field_name,
                "value": field.value,
                "confidence": field.confidence,
                "found": field.found,
                "source": field.source.model_dump() if field.source else None,
            },
        )

    def log_extraction_failed(self, document_type: str, filename: str, reason: str) -> None:
        self._append("extraction_failed", {"document_type": document_type, "filename": filename, "reason": reason})

    def log_sufficiency(self, report: SufficiencyReport) -> None:
        self._append("sufficiency_check", report.model_dump())

    def log_decision_summary(self, summary: dict[str, Any]) -> None:
        self._append("decision_summary", summary)

    def replay(self) -> list[dict[str, Any]]:
        """Return every event logged for this claim, in the order they happened."""
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
