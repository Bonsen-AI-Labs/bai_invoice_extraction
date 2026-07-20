"""Build detailed, JSON-safe eval execution records and aggregate summaries."""

from __future__ import annotations

from collections import defaultdict

from tests.evals import config
from tests.evals.evaluators.accuracy import values_equal
from tests.evals.models import (
    AccuracySummary,
    ColumnAccuracy,
    EvalCase,
    EvaluationBatch,
    EvaluationSummary,
    ExecutionReport,
    ExecutionResult,
    FieldComparison,
    InvoiceEvaluation,
)

_FIELD_TO_COLUMN = {field: column for column, field in config.HEADER_TO_FIELD.items()}


def build_detailed_results(
    batch: EvaluationBatch,
) -> tuple[list[InvoiceEvaluation], EvaluationSummary]:
    invoices = [
        InvoiceEvaluation(
            eval_id=case.eval_id,
            row_number=case.row_number,
            source_path=str(case.pdf_path),
            template_group=case.template_group,
            expected=case.expected,
            baseline=_execution_report(case, batch.baseline.get(case.eval_id)),
            template=_execution_report(case, batch.candidate.get(case.eval_id)),
        )
        for case in batch.cases
    ]
    baseline = _aggregate_accuracy(invoice.baseline for invoice in invoices)
    template = _aggregate_accuracy(invoice.template for invoice in invoices)
    lift = None
    if baseline.accuracy is not None and template.accuracy is not None:
        lift = template.accuracy - baseline.accuracy
    executions = [
        execution
        for invoice in invoices
        for execution in (invoice.baseline, invoice.template)
    ]
    return invoices, EvaluationSummary(
        selected_invoices=len(invoices),
        baseline_successes=sum(invoice.baseline.success for invoice in invoices),
        baseline_failures=sum(not invoice.baseline.success for invoice in invoices),
        template_successes=sum(invoice.template.success for invoice in invoices),
        template_failures=sum(not invoice.template.success for invoice in invoices),
        baseline=baseline,
        template=template,
        template_lift=lift,
        template_lift_percentage_points=_percentage(lift),
        total_di_cost_usd=round(sum(item.di_cost_usd for item in executions), 6),
        total_llm_cost_usd=round(sum(item.llm_cost_usd for item in executions), 6),
        total_duration_seconds=round(
            sum(item.duration_seconds for item in executions), 3
        ),
    )


def _execution_report(
    case: EvalCase, execution: ExecutionResult | None
) -> ExecutionReport:
    if execution is None:
        return ExecutionReport(success=False, error="execution result is missing")
    comparisons: dict[str, FieldComparison] = {}
    if execution.record:
        for field, expected in case.expected.items():
            result = execution.record.fields.get(field)
            actual = result.value if result else None
            comparisons[field] = FieldComparison(
                field=field,
                column=_FIELD_TO_COLUMN.get(field, field),
                expected=expected,
                actual=actual,
                correct=values_equal(field, expected, actual),
                value_raw=result.value_raw if result else "",
                confidence=result.confidence if result else None,
                source_layer=result.source_layer if result else None,
                page=result.page if result else None,
                polygon=result.polygon if result else [],
                candidate_count=len(result.candidates) if result else 0,
                llm_used=bool(result and result.llm),
            )
    else:
        comparisons = {
            field: FieldComparison(
                field=field,
                column=_FIELD_TO_COLUMN.get(field, field),
                expected=expected,
            )
            for field, expected in case.expected.items()
        }
    return ExecutionReport(
        success=execution.record is not None and execution.error is None,
        error=execution.error,
        source_path=execution.source_path,
        content_sha256=execution.content_sha256,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        duration_seconds=execution.duration_seconds,
        di_source=execution.di_source,
        llm_source=execution.llm_source,
        di_cost_usd=execution.di_cost_usd,
        llm_cost_usd=execution.llm_cost_usd,
        accuracy=_accuracy_from_comparisons(comparisons.values()),
        fields=comparisons,
        extraction=execution.record,
    )


def _aggregate_accuracy(executions) -> AccuracySummary:
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for execution in executions:
        if not execution.success:
            continue
        for comparison in execution.fields.values():
            if comparison.correct is None:
                continue
            counts[comparison.field][0] += int(comparison.correct)
            counts[comparison.field][1] += 1
    return _accuracy_from_counts(counts)


def _accuracy_from_comparisons(comparisons) -> AccuracySummary:
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for comparison in comparisons:
        if comparison.correct is None:
            continue
        counts[comparison.field][0] += int(comparison.correct)
        counts[comparison.field][1] += 1
    return _accuracy_from_counts(counts)


def _accuracy_from_counts(counts: dict[str, list[int]]) -> AccuracySummary:
    by_field: dict[str, ColumnAccuracy] = {}
    for field in config.HEADER_TO_FIELD.values():
        correct, total = counts.get(field, [0, 0])
        score = correct / total if total else None
        by_field[field] = ColumnAccuracy(
            field=field,
            column=_FIELD_TO_COLUMN.get(field, field),
            correct=correct,
            total=total,
            accuracy=score,
            accuracy_percent=_percentage(score),
        )
    correct = sum(item.correct for item in by_field.values())
    total = sum(item.total for item in by_field.values())
    score = correct / total if total else None
    return AccuracySummary(
        correct=correct,
        total=total,
        accuracy=score,
        accuracy_percent=_percentage(score),
        by_field=by_field,
    )


def _percentage(value: float | None) -> float | None:
    return round(value * 100, 4) if value is not None else None
