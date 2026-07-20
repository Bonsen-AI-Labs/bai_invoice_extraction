"""Corpus orchestration, training split, evaluation, and promotion gates."""

from __future__ import annotations

import hashlib
import platform
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Literal

from src import config as app_config
from src.models import LearningObservation
from src.template_generation import JsonTemplateStore, TemplateEngine
from tests.evals import config
from tests.evals.corpus import ExcelCorpusReader
from tests.evals.evaluators import (
    ArbitrationRateEvaluator,
    FieldAccuracyEvaluator,
    TemplateCoverageEvaluator,
    TemplateL6PrecisionEvaluator,
    TemplateLiftEvaluator,
)
from tests.evals.evaluators.accuracy import values_equal
from tests.evals.evaluators.template import template_group
from tests.evals.executor import CachePolicy, EvalExecutor
from tests.evals.models import EvalCase, EvalReport, EvalRunMetadata, EvaluationBatch
from tests.evals.reporting import build_detailed_results

EvalCommand = Literal["run", "train", "refresh-cache"]


class EvaluationRunner:
    def __init__(self, *, cache_policy: CachePolicy = "missing") -> None:
        self._cache_policy: CachePolicy = cache_policy

    async def run(
        self,
        *,
        train_templates: bool = False,
        case_limit: int | None = None,
        command: EvalCommand = "run",
    ) -> EvalReport:
        started = perf_counter()
        started_at = datetime.now(timezone.utc)
        run_id = started_at.strftime("%Y%m%dT%H%M%S%fZ")
        corpus = ExcelCorpusReader().read()
        available_cases = len(corpus.cases)
        cases = _select_cases(corpus.cases, case_limit)
        groups = _eligible_groups(cases)
        active_path: Path | None = None

        if train_templates and groups:
            await self._train(groups)
            cases = [case for members in groups.values() for case in members[3:]]
            active_path = Path(app_config.TEMPLATE_CANDIDATE_PATH)

        executor = EvalExecutor(
            cache_policy=self._cache_policy,
            active_template_path=active_path,
        )
        baseline = await executor.execute_many(
            cases,
            template_enabled=False,
            progress_label="Eval baseline",
        )
        candidate = await executor.execute_many(
            cases,
            template_enabled=True,
            progress_label="Eval templates",
        )
        batch = EvaluationBatch(cases=cases, baseline=baseline, candidate=candidate)
        invoices, summary = build_detailed_results(batch)
        evaluators = [
            FieldAccuracyEvaluator(),
            TemplateL6PrecisionEvaluator(),
            TemplateCoverageEvaluator(),
            TemplateLiftEvaluator(),
            ArbitrationRateEvaluator(),
        ]
        metrics = [evaluator.evaluate(batch) for evaluator in evaluators]
        errors = sorted(
            {
                f"{result.eval_id}: {result.error}"
                for collection in (baseline, candidate)
                for result in collection.values()
                if result.error
            }
        )
        status = "PASS"
        if errors:
            status = "FAIL"
        elif not groups:
            status = "INSUFFICIENT_CORPUS"
        else:
            lift = next(
                metric for metric in metrics if metric.name == "template_accuracy_lift"
            )
            group_lift = lift.details.get("group_lift", {})
            if (lift.score is not None and lift.score < 0) or any(
                value < 0 for value in group_lift.values()
            ):
                status = "FAIL"

        finished_at = datetime.now(timezone.utc)
        report_path = config.REPORT_DIR / f"{run_id}.json"
        report = EvalReport(
            run=EvalRunMetadata(
                run_id=run_id,
                started_at=started_at.isoformat(),
                finished_at=finished_at.isoformat(),
                duration_seconds=round(perf_counter() - started, 3),
                command=command,
                cache_policy=self._cache_policy,
                requested_case_limit=case_limit,
                available_case_count=available_cases,
                selected_case_count=len(cases),
                dataset_path=str(config.DATASET_PATH),
                dataset_sha256=_file_sha256(config.DATASET_PATH),
                invoice_directory=str(config.INVOICE_DIR),
                report_path=str(report_path),
                python_version=sys.version.split()[0],
                platform=platform.platform(),
                configuration=_configuration_snapshot(),
            ),
            status=status,
            corpus=corpus,
            summary=summary,
            invoices=invoices,
            metrics=metrics,
            eligible_template_groups=sorted(groups),
            errors=errors,
        )
        config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        payload = report.model_dump_json(indent=2)
        _write_atomic(report_path, payload)
        _write_atomic(config.LATEST_REPORT, payload)
        return report

    async def _train(self, groups: dict[str, list[EvalCase]]) -> None:
        store = JsonTemplateStore()
        store.save_candidate(
            [template.model_copy(deep=True) for template in store.load_active()]
        )
        engine = TemplateEngine(store)
        training = [case for members in groups.values() for case in members[:3]]
        executor = EvalExecutor(cache_policy=self._cache_policy)
        outputs = await executor.execute_many(
            training,
            template_enabled=False,
            progress_label="Train templates",
        )
        for case in training:
            execution = outputs.get(case.eval_id)
            if not execution or not execution.record:
                continue
            record = execution.record
            field_polygons = {
                field: result.polygon
                for field, result in record.fields.items()
                if result.polygon
                and values_equal(field, case.expected.get(field), result.value)
            }
            observation = LearningObservation(
                eval_id=case.eval_id,
                vendor_key=_expected_vendor_key(case),
                confirmed=True,
                field_values=case.expected,
                field_polygons=field_polygons,
            )
            # Recreate the logical invoice from the same replay cache through the
            # executor's extraction result is intentionally avoided; learning needs
            # layout lines. Load it through the small public replay helper.
            inv = await _logical_invoice(case)
            engine.learn_candidate(inv, record, observation)


async def _logical_invoice(case: EvalCase):
    """Restore the cached logical invoice used for offline template learning."""
    import hashlib

    from src.models import OCRSnapshot
    from src.parsing.client import parse, segment
    from src.services.ocr import DIResult

    content_hash = hashlib.sha256(case.pdf_path.read_bytes()).hexdigest()
    path = config.CACHE_DIR / content_hash / "di.json"
    snapshot = OCRSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    invoices = segment(parse(DIResult.from_snapshot(snapshot)))
    if len(invoices) != 1:
        raise ValueError(f"{case.eval_id}: expected one logical invoice")
    return invoices[0]


def _eligible_groups(cases: list[EvalCase]) -> dict[str, list[EvalCase]]:
    grouped: dict[str, list[EvalCase]] = defaultdict(list)
    for case in cases:
        grouped[template_group(case)].append(case)
    required = config.MIN_TEMPLATE_TRAINING_CASES + config.MIN_TEMPLATE_HOLDOUT_CASES
    return {
        group: sorted(members, key=lambda case: case.row_number)
        for group, members in grouped.items()
        if len(members) >= required
    }


def _expected_vendor_key(case: EvalCase) -> str:
    gstin = str(case.expected.get("gstin") or "").replace(" ", "").upper()
    if gstin:
        return gstin
    biller = " ".join(str(case.expected.get("billerName") or "UNKNOWN").upper().split())
    return f"NAME::{biller}"


def _select_cases(cases: list[EvalCase], limit: int | None) -> list[EvalCase]:
    if limit is not None and limit < 1:
        raise ValueError("case limit must be at least 1")
    return cases[:limit] if limit is not None else cases


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _configuration_snapshot() -> dict:
    return {
        "vision_model": app_config.OPENAI_VISION_MODEL,
        "fields": app_config.FIELDS,
        "layer_weights": app_config.LAYER_WEIGHTS,
        "accept_confidence": app_config.ACCEPT_CONF,
        "accept_margin": app_config.ACCEPT_MARGIN,
        "template_active_path": app_config.TEMPLATE_ACTIVE_PATH,
        "template_candidate_path": app_config.TEMPLATE_CANDIDATE_PATH,
        "template_match_min": app_config.TEMPLATE_MATCH_MIN,
        "template_gray_min": app_config.TEMPLATE_GRAY_MIN,
        "minimum_template_training_cases": config.MIN_TEMPLATE_TRAINING_CASES,
        "minimum_template_holdout_cases": config.MIN_TEMPLATE_HOLDOUT_CASES,
    }


def _write_atomic(path: Path, payload: str) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
