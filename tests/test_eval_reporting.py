"""Behavioural coverage for detailed eval reporting and case selection."""

from pathlib import Path

from src.models import Candidate, FieldResult, InvoiceRecord
from tests.evals.evaluation.runner import _select_cases
from tests.evals.models import EvalCase, EvaluationBatch, ExecutionResult
from tests.evals.reporting import build_detailed_results


def _record(eval_id: str, bill_number: str) -> InvoiceRecord:
    return InvoiceRecord(
        eval_id=eval_id,
        fields={
            "billNumber": FieldResult(
                value=bill_number,
                value_raw=bill_number,
                confidence=0.92,
                source_layer="L2",
                page=1,
                polygon=[1, 1, 2, 1, 2, 1.2, 1, 1.2],
                candidates=[
                    Candidate(
                        field="billNumber",
                        value=bill_number,
                        value_raw=bill_number,
                        layers={"L2": 0.92},
                        fused_score=0.92,
                    )
                ],
            ),
            "date": FieldResult(value="2026-07-19", confidence=0.98),
        },
        llm_meta={"called": True, "cost_usd": 0.004},
    )


def _execution(record: InvoiceRecord, *, template_enabled: bool) -> ExecutionResult:
    return ExecutionResult(
        eval_id=record.eval_id,
        record=record,
        template_enabled=template_enabled,
        source_path="data/invoices/example.pdf",
        content_sha256="abc123",
        started_at="2026-07-19T00:00:00+00:00",
        finished_at="2026-07-19T00:00:01+00:00",
        duration_seconds=1.0,
        di_source="live" if not template_enabled else "replay",
        llm_source="live",
        di_cost_usd=0.02 if not template_enabled else 0.0,
        llm_cost_usd=0.004,
    )


def test_detailed_report_retains_expected_extraction_and_column_accuracy() -> None:
    case = EvalCase(
        eval_id="GS-001",
        row_number=2,
        pdf_path=Path("data/invoices/example.pdf"),
        expected={"billNumber": "INV-001", "date": "2026-07-19"},
    )
    baseline = _execution(_record(case.eval_id, "INV-001"), template_enabled=False)
    template = _execution(_record(case.eval_id, "WRONG"), template_enabled=True)

    invoices, summary = build_detailed_results(
        EvaluationBatch(
            cases=[case],
            baseline={case.eval_id: baseline},
            candidate={case.eval_id: template},
        )
    )

    invoice = invoices[0]
    assert invoice.expected == case.expected
    assert invoice.baseline.extraction is not None
    assert invoice.baseline.extraction.fields["billNumber"].candidates
    assert invoice.baseline.fields["billNumber"].correct is True
    assert invoice.template.fields["billNumber"].correct is False
    assert invoice.baseline.accuracy.accuracy_percent == 100.0
    assert invoice.template.accuracy.accuracy_percent == 50.0
    assert summary.baseline.by_field["billNumber"].accuracy_percent == 100.0
    assert summary.template.by_field["billNumber"].accuracy_percent == 0.0
    assert summary.template_lift_percentage_points == -50.0
    assert summary.total_di_cost_usd == 0.02
    assert summary.total_llm_cost_usd == 0.008
    assert '"expected"' in invoice.model_dump_json()
    assert '"extraction"' in invoice.model_dump_json()


def test_case_limit_selects_first_fifty_executable_cases() -> None:
    cases = [
        EvalCase(
            eval_id=f"GS-{index:03d}",
            row_number=index + 1,
            pdf_path=Path(f"invoice-{index}.pdf"),
            expected={},
        )
        for index in range(1, 61)
    ]

    selected = _select_cases(cases, 50)

    assert len(selected) == 50
    assert selected[0].eval_id == "GS-001"
    assert selected[-1].eval_id == "GS-050"
