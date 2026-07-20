"""Phase-1 pipeline orchestrator.

Per new PDF: SHA-256 gate → download → DI → parse+segment → extract (incl. LLM)
→ soft-dup vs Excel rows → Excel upsert → Cosmos history record. Each file runs
inside its own failure boundary; one bad PDF records FAILED_{stage} and the loop
continues. No Blob staging / no resume this phase — a retry re-runs the file.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from src.env import Settings
from src.extraction.client import Extractor
from src.models import HistoryDoc, LearningObservation
from src.parsing.client import SegmentationError, parse, segment
from src.services.cosmos import CosmosClient
from src.services.excel import ExcelClient
from src.services.http import HTTPClient
from src.services.llm import LLMClient
from src.services.ocr import OCRClient
from src.services.sharepoint import SharePointClient
from src.template_generation import JsonTemplateStore, TemplateEngine
from src.utils.logging import get_logger, get_tracer

log = get_logger(__name__)
tracer = get_tracer(__name__)
# ponytail: phase spans live here only; add per-client spans in services/ if a
# specific backend ever needs latency attribution.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _soft_dup_index(rows: list[dict]) -> dict[tuple, str]:
    """Map business-identity tuple -> existing EvalID from the workbook."""
    index: dict[tuple, str] = {}
    for r in rows:
        total = r.get("TotalBillAmount")
        key = (
            r.get("GSTIN") or None,
            r.get("BillNumber") or None,
            r.get("Date") or None,
            round(float(total), 2) if total not in (None, "") else None,
        )
        if any(k is not None for k in key[1:]):
            index[key] = r.get("EvalID", "")
    return index


async def run() -> dict:
    settings = Settings()  # type: ignore[call-arg]  # fields come from the env
    http = HTTPClient(settings)
    sharepoint = SharePointClient(http, settings)
    excel = ExcelClient(http, settings)
    ocr = OCRClient(settings)
    cosmos = CosmosClient(settings)
    templates = TemplateEngine(JsonTemplateStore())
    extractor = Extractor(LLMClient(settings), templates)

    totals = {"processed": 0, "failed": 0, "skipped": 0, "invoices": 0}
    try:
      with tracer.start_as_current_span("pipeline.run"):
        refs = await sharepoint.list_new_pdfs()
        existing = _soft_dup_index(await excel.read_rows())
        log.info("run started", extra={"new_files": len(refs)})
        for ref in refs:
            stage = "STAGED"
            try:
              with tracer.start_as_current_span("process_file") as span:
                span.set_attribute("file", ref.name)
                with tracer.start_as_current_span("download"):
                    pdf = await sharepoint.download(ref)
                sha = hashlib.sha256(pdf).hexdigest()
                ref.sha256 = sha
                span.set_attribute("sha256", sha)
                if await cosmos.is_processed(sha):
                    totals["skipped"] += 1
                    log.info("skipped (already processed)",
                             extra={"file": ref.name, "sha256": sha})
                    continue

                stage = "DI_DONE"
                with tracer.start_as_current_span("ocr.analyze"):
                    di = await ocr.analyze_invoice(pdf)
                stage = "SEGMENTED"
                with tracer.start_as_current_span("segment"):
                    invoices = segment(parse(di))

                eval_ids, tuples, arbitrated = [], [], []
                llm_cost = 0.0
                stage = "EXTRACTED"
                with tracer.start_as_current_span("extract") as ext_span:
                  ext_span.set_attribute("invoices", len(invoices))
                  for inv in invoices:
                    eval_id = f"EVL-{sha[:12]}-{inv.index:02d}"
                    rec = await extractor.extract(inv, pdf, eval_id, ref.path)

                    if settings.TEMPLATE_LIVE_LEARNING and rec.vendor_key:
                        try:
                            templates.learn_candidate(
                                inv,
                                rec,
                                _learning_observation(rec),
                            )
                        except Exception:
                            rec.validation.flags.append("TEMPLATE_LEARN_FAILED")
                            rec.needs_review = True

                    ident = tuple(rec.identity_tuple())
                    if any(k is not None for k in ident[1:]) and ident in existing:
                        rec.duplicate_suspect_of = existing[ident]
                        rec.needs_review = True
                        rec.validation.flags.append("DUP?")

                    await excel.upsert_row(eval_id, rec.workbook_row())
                    existing[ident] = eval_id
                    eval_ids.append(eval_id)
                    tuples.append(list(rec.identity_tuple()))
                    arbitrated += rec.llm_meta.get("fields", [])
                    llm_cost += rec.llm_meta.get("cost_usd", 0.0)

                await cosmos.record_history(
                    HistoryDoc(
                        id=sha,
                        sha256=sha,
                        file_name=ref.name,
                        file_path=ref.path,
                        outcome="DONE",
                        stage="SHEET_WRITTEN",
                        eval_ids=eval_ids,
                        identity_tuples=tuples,
                        di_cost_usd=di.cost_usd,
                        llm_cost_usd=llm_cost,
                        llm_fields_arbitrated=arbitrated,
                        timestamps={"ingested": _now(), "sheetWritten": _now()},
                    )
                )
                totals["processed"] += 1
                totals["invoices"] += len(invoices)
                log.info("processed",
                         extra={"file": ref.name, "sha256": sha,
                                "invoices": len(invoices), "eval_ids": eval_ids})

            except SegmentationError as e:
                log.warning("segmentation failed, parking",
                            extra={"file": ref.name, "stage": stage, "error": str(e)})
                await _park(cosmos, excel, ref, "FAILED_SEGMENTED", str(e))
                totals["failed"] += 1
            except Exception as e:  # per-file isolation
                log.exception("file failed, parking",
                              extra={"file": ref.name, "stage": stage})
                await _park(cosmos, excel, ref, f"FAILED_{stage}", repr(e))
                totals["failed"] += 1
    finally:
        await http.aclose()
        await ocr.aclose()
        await cosmos.aclose()
    return totals


def _learning_observation(rec) -> LearningObservation:
    validation_consistent = not any(
        verdict == "FAIL"
        for verdict in (rec.validation.arith, rec.validation.tax, rec.validation.date)
    )
    llm_confident = any(
        result.llm and float(result.llm.get("confidence", 0.0)) >= 0.9
        for result in rec.fields.values()
    )
    confirmed = rec.validation.arith == "PASS" or (
        validation_consistent and llm_confident
    )
    return LearningObservation(
        eval_id=rec.eval_id,
        vendor_key=rec.vendor_key or "",
        confirmed=confirmed,
        field_values={key: result.value for key, result in rec.fields.items()},
        field_polygons={
            key: result.polygon for key, result in rec.fields.items() if result.polygon
        },
    )


async def _park(
    cosmos: CosmosClient, excel: ExcelClient, ref, outcome: str, error: str
):
    sha = ref.sha256 or hashlib.sha256(ref.item_id.encode()).hexdigest()
    try:
        await cosmos.record_history(
            HistoryDoc(
                id=sha,
                sha256=sha,
                file_name=ref.name,
                file_path=ref.path,
                outcome=outcome,
                error=error,
                timestamps={"ingested": _now()},
            )
        )
        await excel.add_failure(
            {
                "FileName": ref.name,
                "FilePath": ref.path,
                "Sha256": sha,
                "Stage": outcome,
                "Error": error,
                "LastAttempt": _now(),
            }
        )
    except Exception:
        log.exception("parking failed", extra={"file": ref.name, "outcome": outcome})
        # parking must never raise
