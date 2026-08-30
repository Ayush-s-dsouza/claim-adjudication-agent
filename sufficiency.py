"""Deterministic sufficiency rules for a death claim.

No model calls anywhere in this file. Every rule below is a plain constant
or a plain comparison — this is the part of the system meant to be
inspectable by a regulator or an auditor without trusting a model's
reasoning. `extract.py` is where the model call lives; this file only ever
reads its output.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from schema import ClaimFile, DocumentType, MismatchFlag, SufficiencyReport

# Every document type required for a death claim to be decision-ready.
# This demo covers exactly one claim type (death claim); a real system
# would branch this on claim type and circumstances (e.g. discharge summary
# only required when death occurred in hospital).
REQUIRED_DOCUMENTS: list[DocumentType] = [
    DocumentType.DEATH_CERTIFICATE,
    DocumentType.HOSPITAL_DISCHARGE_SUMMARY,
    DocumentType.CLAIM_INTIMATION_FORM,
    DocumentType.NOMINEE_KYC,
]

# Fields each document must yield for the claim to be considered complete.
# Field names are the normalized semantic names extract.py assigns, not the
# literal labels printed on the document (e.g. a discharge summary's
# "Patient Name" is extracted into "deceased_name" so it can be compared
# against the same field on other documents).
REQUIRED_FIELDS_BY_DOCUMENT: dict[DocumentType, list[str]] = {
    DocumentType.DEATH_CERTIFICATE: [
        "deceased_name",
        "date_of_death",
        "registration_number",
        "place_of_death",
    ],
    DocumentType.HOSPITAL_DISCHARGE_SUMMARY: [
        "deceased_name",
        "cause_of_death",
        "date_of_admission",
    ],
    DocumentType.CLAIM_INTIMATION_FORM: [
        "deceased_name",
        "claimant_name",
        "relationship_to_deceased",
        "policy_number",
    ],
    DocumentType.NOMINEE_KYC: [
        "claimant_name",
        "id_type",
        "id_number",
    ],
}

# Fields expected to match across every document that reports them.
MISMATCH_CHECK_FIELDS: list[str] = ["deceased_name", "claimant_name"]

# Below this confidence, a field is flagged for human review even though
# Sarvam did return a value. Chosen as a conservative default for a demo,
# not derived from any measured accuracy data.
CONFIDENCE_THRESHOLD = 0.80

# Below this string similarity (0-1, via difflib), two occurrences of the
# same field are considered a mismatch worth flagging rather than a harmless
# spelling/spacing variant.
MISMATCH_SIMILARITY_THRESHOLD = 0.85


def _normalize(value: str) -> str:
    return " ".join(value.strip().lower().split())


def check_sufficiency(claim: ClaimFile) -> SufficiencyReport:
    missing_documents = [doc_type for doc_type in REQUIRED_DOCUMENTS if doc_type not in claim.documents]

    not_found_fields: list[tuple[DocumentType, str]] = []
    low_confidence_fields: list[tuple[DocumentType, str, float]] = []
    for doc_type, document in claim.documents.items():
        for field_name in REQUIRED_FIELDS_BY_DOCUMENT.get(doc_type, []):
            field = document.fields.get(field_name)
            if field is None or not field.found:
                not_found_fields.append((doc_type, field_name))
            elif field.confidence < CONFIDENCE_THRESHOLD:
                low_confidence_fields.append((doc_type, field_name, field.confidence))

    mismatches: list[MismatchFlag] = []
    for field_name in MISMATCH_CHECK_FIELDS:
        occurrences = [
            (doc_type, str(field.value))
            for doc_type, field in claim.field_across_documents(field_name)
            if field.value
        ]
        if len(occurrences) < 2:
            continue
        normalized = [_normalize(value) for _, value in occurrences]
        min_ratio = min(
            SequenceMatcher(None, normalized[i], normalized[j]).ratio()
            for i in range(len(normalized))
            for j in range(i + 1, len(normalized))
        )
        if min_ratio < MISMATCH_SIMILARITY_THRESHOLD:
            mismatches.append(
                MismatchFlag(field_name=field_name, occurrences=occurrences, similarity=round(min_ratio, 3))
            )

    is_decision_ready = not (missing_documents or not_found_fields or low_confidence_fields or mismatches)

    return SufficiencyReport(
        claim_id=claim.claim_id,
        missing_documents=missing_documents,
        low_confidence_fields=low_confidence_fields,
        not_found_fields=not_found_fields,
        mismatches=mismatches,
        is_decision_ready=is_decision_ready,
    )
