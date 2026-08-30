"""CLI: point at a folder of claim documents, get a decision-ready summary.

    python run.py samples/claim_001

This wires together the three pieces deliberately kept separate:
extract.py (the one model call), sufficiency.py (deterministic rules), and
audit.py (the replayable log of both). This file itself makes no decisions
and calls no model -- it only orchestrates and prints.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from audit import AuditLog
from extract import ExtractionError, SarvamExtractor
from schema import ClaimFile, DocumentType, SufficiencyReport
from sufficiency import check_sufficiency


def discover_documents(claim_dir: Path) -> dict[DocumentType, Path]:
    """Match files in the folder to document types by exact filename stem
    (e.g. death_certificate.png -> DocumentType.DEATH_CERTIFICATE)."""
    by_stem = {doc_type.value: doc_type for doc_type in DocumentType}
    found: dict[DocumentType, Path] = {}
    for path in sorted(claim_dir.iterdir()):
        if path.is_file() and path.stem.lower() in by_stem:
            found[by_stem[path.stem.lower()]] = path
    return found


def format_summary(claim: ClaimFile, report: SufficiencyReport) -> str:
    lines = [f"Claim {claim.claim_id}", "=" * (len(claim.claim_id) + 6)]

    for doc_type in DocumentType:
        document = claim.documents.get(doc_type)
        if document is None:
            continue
        lines.append(f"\n{doc_type.value} ({document.filename})")
        for field_name, field in document.fields.items():
            if field.found:
                page = field.source.page_num if field.source else "?"
                lines.append(f"  {field_name}: {field.value!r}  (confidence {field.confidence:.2f}, page {page})")
            else:
                lines.append(f"  {field_name}: NOT FOUND")

    lines.append("\n--- Sufficiency ---")
    if report.missing_documents:
        lines.append("Missing documents:")
        lines.extend(f"  - {doc_type.value}" for doc_type in report.missing_documents)
    if report.not_found_fields:
        lines.append("Fields not found:")
        lines.extend(f"  - {doc_type.value}.{field_name}" for doc_type, field_name in report.not_found_fields)
    if report.low_confidence_fields:
        lines.append("Low-confidence fields (needs human check):")
        lines.extend(
            f"  - {doc_type.value}.{field_name} (confidence {confidence:.2f})"
            for doc_type, field_name, confidence in report.low_confidence_fields
        )
    if report.mismatches:
        lines.append("Mismatches across documents (flagged, NOT auto-resolved):")
        for mismatch in report.mismatches:
            occurrences = ", ".join(f"{doc_type.value}={value!r}" for doc_type, value in mismatch.occurrences)
            lines.append(f"  - {mismatch.field_name}: {occurrences} (similarity {mismatch.similarity})")
    if not (report.missing_documents or report.not_found_fields or report.low_confidence_fields or report.mismatches):
        lines.append("Nothing outstanding.")

    lines.append("")
    if report.is_decision_ready:
        lines.append("STATUS: decision-ready for human adjudicator review.")
    else:
        lines.append("STATUS: NOT decision-ready -- needs human follow-up before adjudication.")

    return "\n".join(lines)


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Get a claim folder decision-ready for a human adjudicator.")
    parser.add_argument("claim_folder", type=Path, help="Folder containing one claim's documents")
    parser.add_argument("--language", default="en-IN", help="BCP-47 language code passed to Sarvam (default: en-IN)")
    args = parser.parse_args()

    if not args.claim_folder.is_dir():
        parser.error(f"{args.claim_folder} is not a directory")

    try:
        extractor = SarvamExtractor()
    except ExtractionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    claim_id = args.claim_folder.name
    audit_log = AuditLog(claim_id)
    claim = ClaimFile(claim_id=claim_id)

    documents = discover_documents(args.claim_folder)
    if not documents:
        print(f"warning: no recognized documents found in {args.claim_folder}", file=sys.stderr)

    for doc_type, path in documents.items():
        print(f"extracting {doc_type.value} ({path.name})...")
        try:
            extracted = extractor.extract_document(doc_type, path, language=args.language)
        except ExtractionError as e:
            audit_log.log_extraction_failed(doc_type.value, path.name, str(e))
            print(f"  failed: {e}", file=sys.stderr)
            continue
        claim.documents[doc_type] = extracted
        for field in extracted.fields.values():
            audit_log.log_field_extracted(doc_type.value, path.name, field)

    report = check_sufficiency(claim)
    audit_log.log_sufficiency(report)
    audit_log.log_decision_summary({"is_decision_ready": report.is_decision_ready})

    print()
    print(format_summary(claim, report))
    print(f"\nAudit trail: {audit_log.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
