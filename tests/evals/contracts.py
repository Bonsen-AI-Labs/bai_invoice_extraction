"""Deterministic contract checks for templates and eval infrastructure."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from src.models import (
    BBoxPrior,
    FieldResult,
    InvoiceRecord,
    LearningObservation,
    OCRSnapshot,
)
from src.parsing.client import (
    Line,
    LogicalInvoice,
    PageInfo,
    ParsedDocument,
    parse,
    segment,
)
from src.services.ocr import DIResult
from src.template_generation import JsonTemplateStore, TemplateEngine
from src.template_generation.client import induce_pattern
from tests.evals.corpus import ExcelCorpusReader
from tests.evals.executor import _dispute_signature
from tests.evals.models import EvalReport, EvalRunMetadata


def _invoice(number: str = "INV-001") -> LogicalInvoice:
    lines = [
        Line("TAX INVOICE", 1, [3, 0.2, 5, 0.2, 5, 0.5, 3, 0.5]),
        Line("27AAPFU0939F1ZV", 1, [0.5, 0.8, 2.2, 0.8, 2.2, 1, 0.5, 1]),
        Line(number, 1, [6, 1, 7.2, 1, 7.2, 1.3, 6, 1.3]),
    ]
    di = DIResult(
        invoice=SimpleNamespace(documents=[]), layout=None, pages=1, cost_usd=0.0
    )
    parsed = ParsedDocument(
        pages=[PageInfo(1, 8.5, 11.0, "inch", 0.0)],
        lines=lines,
        blocks=[],
        tables=[],
        di=di,
    )
    return LogicalInvoice(1, [1, 1], None, lines, [], [], parsed)


def _record(eval_id: str, number: str) -> InvoiceRecord:
    return InvoiceRecord(
        eval_id=eval_id,
        vendor_key="27AAPFU0939F1ZV",
        fields={
            "billNumber": FieldResult(
                value=number,
                confidence=1.0,
                page=1,
                polygon=[6, 1, 7.2, 1, 7.2, 1.3, 6, 1.3],
            )
        },
    )


def check_welford() -> None:
    prior = BBoxPrior()
    prior.update([1.0, 2.0, 3.0, 4.0])
    prior.update([3.0, 4.0, 5.0, 6.0])
    assert prior.n == 2
    assert prior.mean == [2.0, 3.0, 4.0, 5.0]
    assert prior.m2 == [2.0, 2.0, 2.0, 2.0]


def check_pattern_induction() -> None:
    pattern = induce_pattern(["INV-001", "INV-002", "INV-103"])
    assert pattern is not None
    import re

    assert re.fullmatch(pattern, "INV-999")
    assert not re.fullmatch(pattern, "BILL-999")


def check_template_learning_and_promotion() -> None:
    with TemporaryDirectory() as directory:
        active = Path(directory) / "active.json"
        candidate = Path(directory) / "candidate.json"
        store = JsonTemplateStore(active, candidate)
        engine = TemplateEngine(store)
        for index, number in enumerate(("INV-001", "INV-002", "INV-103"), 1):
            record = _record(f"EVL-{index}", number)
            engine.learn_candidate(
                _invoice(number),
                record,
                LearningObservation(
                    eval_id=record.eval_id,
                    vendor_key="27AAPFU0939F1ZV",
                    confirmed=True,
                    field_values={"billNumber": number},
                    field_polygons={"billNumber": record.fields["billNumber"].polygon},
                ),
            )
        learned = store.load_candidate()
        assert len(learned) == 1
        assert learned[0].fields["billNumber"].bbox_prior.n == 3
        assert learned[0].fields["billNumber"].value_pattern.active
        store.promote()
        match, candidates = TemplateEngine(store).propose(_invoice("INV-999"))
        assert match.verdict == "MATCH"
        assert any(candidate.field == "billNumber" for candidate in candidates)


def check_corpus_contract() -> None:
    corpus = ExcelCorpusReader().read()
    assert corpus.total_rows == 190
    assert len(corpus.cases) == 30
    assert corpus.executable_rows == 30
    assert corpus.skipped_rows == 160


def check_cache_signature() -> None:
    dispute = {
        "field": "billNumber",
        "definition": "invoice identifier",
        "candidates": [{"value": "INV-1", "source": "L2"}],
        "crops": [b"png"],
        "note": "",
    }
    first = _dispute_signature([dispute])
    assert first == _dispute_signature([dict(dispute)])
    changed = dict(dispute)
    changed["candidates"] = [{"value": "INV-2", "source": "L2"}]
    assert first != _dispute_signature([changed])


def check_report_redacts_labels() -> None:
    corpus = ExcelCorpusReader().read()
    report = EvalReport(
        run=EvalRunMetadata(
            run_id="test",
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:01+00:00",
            duration_seconds=1.0,
            command="run",
            cache_policy="replay",
            dataset_path="dataset.xlsx",
            dataset_sha256="abc",
            invoice_directory="invoices",
            report_path="report.json",
            python_version="3.14",
            platform="test",
        ),
        status="INSUFFICIENT_CORPUS",
        corpus=corpus,
    )
    payload = report.model_dump_json()
    assert '"cases"' not in payload
    assert "Hala Taxi" not in payload


def check_di_snapshot_round_trip() -> None:
    snapshot = OCRSnapshot(
        invoice={"documents": []},
        layout={
            "pages": [
                {
                    "pageNumber": 1,
                    "width": 8.5,
                    "height": 11.0,
                    "unit": "inch",
                    "angle": 0.0,
                    "lines": [
                        {
                            "content": "TAX INVOICE",
                            "polygon": [0, 0, 1, 0, 1, 0.2, 0, 0.2],
                        }
                    ],
                }
            ],
            "tables": [],
        },
        pages=1,
        cost_usd=0.02,
    )
    logical = segment(parse(DIResult.from_snapshot(snapshot)))
    assert len(logical) == 1
    assert logical[0].full_text == "TAX INVOICE"

    field_snapshot = OCRSnapshot(
        invoice={
            "documents": [
                {
                    "fields": {
                        "VendorName": {
                            "type": "string",
                            "valueString": "Example Vendor",
                            "content": "Example Vendor",
                            "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                        }
                    }
                }
            ]
        },
        layout={"pages": [], "tables": []},
        pages=0,
        cost_usd=0.0,
    )
    restored = DIResult.from_snapshot(field_snapshot)
    vendor = restored.invoice.documents[0].fields["VendorName"]
    assert vendor.value_string == "Example Vendor"
    assert vendor.bounding_regions[0].page_number == 1


def main() -> None:
    checks = [
        check_welford,
        check_pattern_induction,
        check_template_learning_and_promotion,
        check_corpus_contract,
        check_cache_signature,
        check_report_redacts_labels,
        check_di_snapshot_round_trip,
    ]
    for check in checks:
        check()
    print(f"eval contracts ok: {len(checks)} checks")


if __name__ == "__main__":
    main()
