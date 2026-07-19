"""Pydantic domain models shared across clients and the engine.

Canonical shapes: Candidate, the invoice fields, and the persisted document.
Phase 1 stores the extracted values in the master
Excel workbook and only an execution-history document in Cosmos.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FileRef(BaseModel):
    """A PDF discovered in the SharePoint drop folder."""

    name: str
    drive_id: str
    item_id: str
    path: str
    size: int = 0
    last_modified: str | None = None
    sha256: str | None = None  # filled after download


class Candidate(BaseModel):
    """One layer's proposal for a field value."""

    field: str
    value: Any = None  # normalized (amount→float, date→ISO str)
    value_raw: str = ""  # exact OCR string
    page: int = 1
    polygon: list[float] = Field(default_factory=list)  # inches, 8 floats
    layers: dict[str, float] = Field(default_factory=dict)  # {"L1": 0.9, ...}
    fused_score: float = 0.0
    evidence: dict[str, Any] = Field(default_factory=dict)


class FieldResult(BaseModel):
    """Resolved value for one field plus provenance."""

    value: Any = None
    value_raw: str = ""
    confidence: float = 0.0
    source_layer: str = ""
    page: int | None = None
    polygon: list[float] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)  # top-3
    llm: dict[str, Any] | None = None


class ValidationResult(BaseModel):
    """Deterministic validation verdicts."""

    arith: str = "UNDECIDABLE"  # PASS / FAIL / UNDECIDABLE
    gstin: str = "UNDECIDABLE"
    tax: str = "UNDECIDABLE"
    date: str = "UNDECIDABLE"
    items: str = "UNDECIDABLE"
    currency: str = "UNDECIDABLE"  # EXPLICIT / INFERRED / UNDECIDABLE
    flags: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class InvoiceRecord(BaseModel):
    """One logical invoice's extracted result."""

    eval_id: str
    vendor_key: str | None = None
    file_path: str = ""
    page_range: list[int] = Field(default_factory=list)
    fields: dict[str, FieldResult] = Field(default_factory=dict)
    validation: ValidationResult = Field(default_factory=ValidationResult)
    needs_review: bool = False
    duplicate_suspect_of: str | None = None
    llm_meta: dict[str, Any] = Field(default_factory=dict)  # called, fields, cost

    def identity_tuple(self) -> tuple:
        """Business identity for soft-duplicate detection."""

        def val(f):
            fr = self.fields.get(f)
            return fr.value if fr else None

        total = val("totalBillAmount")
        return (
            self.vendor_key,
            val("billNumber"),
            val("date"),
            round(float(total), 2) if total is not None else None,
        )

    def workbook_row(self) -> dict[str, Any]:
        """Flatten to {workbook-header: value} for the master Excel table."""
        from src.config import FIELD_TO_HEADER

        r: dict[str, Any] = {
            "EvalID": self.eval_id,
            "FileName": self.file_path.rsplit("/", 1)[-1],
            "FilePath": self.file_path,
        }
        for key, header in FIELD_TO_HEADER.items():
            fr = self.fields.get(key)
            r[header] = fr.value if fr else None
        r["ValidationStatus"] = ";".join(self.validation.flags) or "OK"
        r["LLMUsed"] = "Y" if self.llm_meta.get("called") else "N"
        r["NeedsReview"] = "Y" if self.needs_review else "N"
        r["DuplicateSuspectOf"] = self.duplicate_suspect_of or ""
        return r


class HistoryDoc(BaseModel):
    """Per-file execution history persisted to Cosmos (the only Cosmos write)."""

    id: str  # = sha256 (point-read dedup gate)
    type: str = "history"
    sha256: str
    file_name: str = ""
    file_path: str = ""
    outcome: str = "DONE"  # DONE / FAILED_{STAGE} / PARKED
    stage: str = ""
    eval_ids: list[str] = Field(default_factory=list)
    identity_tuples: list[list] = Field(default_factory=list)  # for soft-dup
    di_cost_usd: float = 0.0
    llm_cost_usd: float = 0.0
    llm_fields_arbitrated: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    error: str | None = None
    timestamps: dict[str, str] = Field(default_factory=dict)
