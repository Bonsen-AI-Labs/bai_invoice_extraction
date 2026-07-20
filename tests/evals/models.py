from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.models import InvoiceRecord


class EvalCase(BaseModel):
    eval_id: str
    row_number: int
    pdf_path: Path
    expected: dict[str, Any]
    template_group: str | None = None


class CorpusResult(BaseModel):
    cases: list[EvalCase] = Field(default_factory=list, exclude=True)
    total_rows: int = 0
    executable_rows: int = 0
    skipped_rows: int = 0
    issues: list[str] = Field(default_factory=list)


class ExecutionResult(BaseModel):
    eval_id: str
    record: InvoiceRecord | None = None
    error: str | None = None
    template_enabled: bool = False
    source_path: str = ""
    content_sha256: str | None = None
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    di_source: Literal["live", "replay"] | None = None
    llm_source: Literal["live", "replay"] | None = None
    di_cost_usd: float = 0.0
    llm_cost_usd: float = 0.0


class EvaluationBatch(BaseModel):
    cases: list[EvalCase]
    baseline: dict[str, ExecutionResult]
    candidate: dict[str, ExecutionResult]


class EvaluatorResult(BaseModel):
    name: str
    score: float | None = None
    numerator: int = 0
    denominator: int = 0
    details: dict[str, Any] = Field(default_factory=dict)


class FieldComparison(BaseModel):
    field: str
    column: str
    expected: Any = None
    actual: Any = None
    correct: bool | None = None
    value_raw: str = ""
    confidence: float | None = None
    source_layer: str | None = None
    page: int | None = None
    polygon: list[float] = Field(default_factory=list)
    candidate_count: int = 0
    llm_used: bool = False


class ColumnAccuracy(BaseModel):
    field: str
    column: str
    correct: int = 0
    total: int = 0
    accuracy: float | None = None
    accuracy_percent: float | None = None


class AccuracySummary(BaseModel):
    correct: int = 0
    total: int = 0
    accuracy: float | None = None
    accuracy_percent: float | None = None
    by_field: dict[str, ColumnAccuracy] = Field(default_factory=dict)


class ExecutionReport(BaseModel):
    success: bool
    error: str | None = None
    source_path: str = ""
    content_sha256: str | None = None
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    di_source: Literal["live", "replay"] | None = None
    llm_source: Literal["live", "replay"] | None = None
    di_cost_usd: float = 0.0
    llm_cost_usd: float = 0.0
    accuracy: AccuracySummary = Field(default_factory=AccuracySummary)
    fields: dict[str, FieldComparison] = Field(default_factory=dict)
    extraction: InvoiceRecord | None = None


class InvoiceEvaluation(BaseModel):
    eval_id: str
    row_number: int
    source_path: str
    template_group: str | None = None
    expected: dict[str, Any] = Field(default_factory=dict)
    baseline: ExecutionReport
    template: ExecutionReport


class EvaluationSummary(BaseModel):
    selected_invoices: int = 0
    baseline_successes: int = 0
    baseline_failures: int = 0
    template_successes: int = 0
    template_failures: int = 0
    baseline: AccuracySummary = Field(default_factory=AccuracySummary)
    template: AccuracySummary = Field(default_factory=AccuracySummary)
    template_lift: float | None = None
    template_lift_percentage_points: float | None = None
    total_di_cost_usd: float = 0.0
    total_llm_cost_usd: float = 0.0
    total_duration_seconds: float = 0.0


class EvalRunMetadata(BaseModel):
    run_id: str
    started_at: str
    finished_at: str
    duration_seconds: float
    command: Literal["run", "train", "refresh-cache"]
    cache_policy: Literal["replay", "missing", "refresh"]
    requested_case_limit: int | None = None
    available_case_count: int = 0
    selected_case_count: int = 0
    dataset_path: str
    dataset_sha256: str
    invoice_directory: str
    report_path: str
    python_version: str
    platform: str
    configuration: dict[str, Any] = Field(default_factory=dict)


class EvalReport(BaseModel):
    schema_version: int = 2
    run: EvalRunMetadata
    status: Literal["PASS", "FAIL", "INSUFFICIENT_CORPUS"]
    corpus: CorpusResult
    summary: EvaluationSummary = Field(default_factory=EvaluationSummary)
    invoices: list[InvoiceEvaluation] = Field(default_factory=list)
    metrics: list[EvaluatorResult] = Field(default_factory=list)
    eligible_template_groups: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
