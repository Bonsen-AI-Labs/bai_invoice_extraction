"""Pydantic domain models shared across clients and the engine.

Canonical shapes: Candidate, the invoice fields, and the persisted document.
Phase 1 stores the extracted values in the master
Excel workbook and only an execution-history document in Cosmos.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

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
    template_id: str | None = None
    template_score: float | None = None
    template_verdict: str | None = None

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
        r["TemplateId"] = self.template_id or ""
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


class StaticToken(BaseModel):
    """A stable text token in normalised page coordinates."""

    text: str
    cx: float
    cy: float


class TemplateFingerprint(BaseModel):
    """Coarse and precise layout identity for one vendor layout."""

    grid_bits_hex: str = "0"
    static_tokens: list[StaticToken] = Field(default_factory=list)
    table_col_centres: list[float] = Field(default_factory=list)
    table_header_tokens: list[str] = Field(default_factory=list)
    registration_anchors: list[str] = Field(default_factory=list)
    elastic_zones: list[dict[str, Any]] = Field(default_factory=list)


class BBoxPrior(BaseModel):
    """Online Welford statistics for cx, cy, width, and height."""

    n: int = 0
    mean: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    m2: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])

    def update(self, point: list[float], weight: int = 1) -> None:
        if len(point) != 4:
            raise ValueError("bbox point must contain cx, cy, width, height")
        for _ in range(max(weight, 1)):
            self.n += 1
            for index, value in enumerate(point):
                delta = value - self.mean[index]
                self.mean[index] += delta / self.n
                self.m2[index] += delta * (value - self.mean[index])


class ValuePattern(BaseModel):
    regex: str | None = None
    samples: list[str] = Field(default_factory=list)
    active: bool = False
    violations: int = 0


class TemplateFieldPrior(BaseModel):
    anchor_text: str | None = None
    anchor_relation: str | None = None
    bbox_prior: BBoxPrior = Field(default_factory=BBoxPrior)
    value_pattern: ValuePattern = Field(default_factory=ValuePattern)
    hit: int = 0
    miss: int = 0
    consecutive_miss: int = 0


class TemplateStats(BaseModel):
    instances_seen: int = 0
    last_seen: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    created_from_eval_id: str = ""
    health: Literal["ACTIVE", "DORMANT", "QUARANTINED"] = "QUARANTINED"
    match_score_ewma: float = 0.0


class TemplateDocument(BaseModel):
    id: str
    vendor_key: str
    variant: int
    fingerprint: TemplateFingerprint
    fields: dict[str, TemplateFieldPrior] = Field(default_factory=dict)
    validation_profile: dict[str, Any] = Field(default_factory=dict)
    stats: TemplateStats = Field(default_factory=TemplateStats)
    schema_version: int = 1


class TemplateMatch(BaseModel):
    template_id: str | None = None
    vendor_key: str | None = None
    score: float = 0.0
    stage_a_score: float = 0.0
    verdict: Literal["MATCH", "GRAY_ZONE", "NEW_VARIANT", "NO_TEMPLATE"] = "NO_TEMPLATE"
    registration_skipped: bool = False


class LearningObservation(BaseModel):
    eval_id: str
    vendor_key: str
    confirmed: bool
    correction_weight: int = 1
    field_values: dict[str, Any] = Field(default_factory=dict)
    field_polygons: dict[str, list[float]] = Field(default_factory=dict)


class OCRSnapshot(BaseModel):
    """JSON-safe representation of the paid DI responses for replay evals."""

    invoice: dict[str, Any]
    layout: dict[str, Any]
    pages: int
    cost_usd: float
