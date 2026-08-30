"""Pydantic models for a life-insurance death claim file.

These are plain data models. Nothing here calls Sarvam or any other model —
that split is the point (see README). `extract.py` populates these models
from Sarvam's Extract API; `sufficiency.py` reads them with deterministic
rules only.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class DocumentType(str, Enum):
    DEATH_CERTIFICATE = "death_certificate"
    HOSPITAL_DISCHARGE_SUMMARY = "hospital_discharge_summary"
    CLAIM_INTIMATION_FORM = "claim_intimation_form"
    NOMINEE_KYC = "nominee_kyc"


class SourcePointer(BaseModel):
    """Where an extracted value came from. Page-level only.

    Sarvam's Extract API returns document + page per field (see
    `doc_ai.get_results` -> `annotations[...].sources`), not a bounding box
    or region within the page. Don't claim finer granularity than that.
    """

    document_type: DocumentType
    filename: str
    page_num: Optional[int] = None


class ExtractedField(BaseModel):
    """One field pulled from one document, with its confidence and source.

    `value` is `None` and `confidence` is `0.0` when Sarvam didn't find the
    field at all — that's a real, distinct state from "found but not sure",
    and `sufficiency.py` treats them differently.
    """

    field_name: str
    value: Optional[Any] = None
    confidence: float = 0.0
    source: Optional[SourcePointer] = None
    found: bool = True


class ExtractedDocument(BaseModel):
    """All fields extracted from a single physical document."""

    document_type: DocumentType
    filename: str
    fields: dict[str, ExtractedField] = Field(default_factory=dict)


class ClaimFile(BaseModel):
    """Everything extracted so far for one claim, across all its documents."""

    claim_id: str
    documents: dict[DocumentType, ExtractedDocument] = Field(default_factory=dict)

    def field_across_documents(self, field_name: str) -> list[tuple[DocumentType, ExtractedField]]:
        """All occurrences of a field name across every document in the claim.

        Used by `sufficiency.py` to compare, e.g., the deceased's name as it
        appears on the death certificate vs. the discharge summary.
        """
        out = []
        for doc_type, doc in self.documents.items():
            field = doc.fields.get(field_name)
            if field is not None and field.found:
                out.append((doc_type, field))
        return out


class MismatchFlag(BaseModel):
    field_name: str
    occurrences: list[tuple[DocumentType, str]]
    similarity: float


class SufficiencyReport(BaseModel):
    """Deterministic verdict on whether a claim is decision-ready.

    Produced entirely by `sufficiency.py` — no model call, no confidence
    scores of its own. Every field here traces back to a fixed rule.
    """

    claim_id: str
    missing_documents: list[DocumentType] = Field(default_factory=list)
    low_confidence_fields: list[tuple[DocumentType, str, float]] = Field(default_factory=list)
    not_found_fields: list[tuple[DocumentType, str]] = Field(default_factory=list)
    mismatches: list[MismatchFlag] = Field(default_factory=list)
    is_decision_ready: bool = False
