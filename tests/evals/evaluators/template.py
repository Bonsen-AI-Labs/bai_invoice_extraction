"""Held-out behavioural measurements for the L6 template system."""

from __future__ import annotations

from collections import defaultdict

from tests.evals.evaluators.accuracy import field_accuracy, values_equal
from tests.evals.evaluators.base import BaseEvaluator
from tests.evals.models import EvalCase, EvaluationBatch, EvaluatorResult


class TemplateL6PrecisionEvaluator(BaseEvaluator):
    @property
    def name(self) -> str:
        return "template_l6_precision"

    def evaluate(self, batch: EvaluationBatch) -> EvaluatorResult:
        correct = total = 0
        for case in batch.cases:
            execution = batch.candidate.get(case.eval_id)
            if not execution or not execution.record:
                continue
            for field, result in execution.record.fields.items():
                expected = case.expected.get(field)
                for candidate in result.candidates:
                    if "L6" not in candidate.layers:
                        continue
                    total += 1
                    correct += int(values_equal(field, expected, candidate.value))
        return EvaluatorResult(
            name=self.name,
            score=correct / total if total else None,
            numerator=correct,
            denominator=total,
        )


class TemplateCoverageEvaluator(BaseEvaluator):
    @property
    def name(self) -> str:
        return "template_correct_coverage"

    def evaluate(self, batch: EvaluationBatch) -> EvaluatorResult:
        covered = labelled = 0
        for case in batch.cases:
            execution = batch.candidate.get(case.eval_id)
            if not execution or not execution.record:
                continue
            for field, expected in case.expected.items():
                if expected in (None, ""):
                    continue
                labelled += 1
                result = execution.record.fields.get(field)
                if result and any(
                    "L6" in candidate.layers
                    and values_equal(field, expected, candidate.value)
                    for candidate in result.candidates
                ):
                    covered += 1
        return EvaluatorResult(
            name=self.name,
            score=covered / labelled if labelled else None,
            numerator=covered,
            denominator=labelled,
        )


class TemplateLiftEvaluator(BaseEvaluator):
    @property
    def name(self) -> str:
        return "template_accuracy_lift"

    def evaluate(self, batch: EvaluationBatch) -> EvaluatorResult:
        base_hits, base_total, _ = field_accuracy(batch, candidate=False)
        candidate_hits, candidate_total, _ = field_accuracy(batch, candidate=True)
        base_score = base_hits / base_total if base_total else 0.0
        candidate_score = candidate_hits / candidate_total if candidate_total else 0.0
        group_lift = self._group_lift(batch)
        return EvaluatorResult(
            name=self.name,
            score=candidate_score - base_score,
            numerator=candidate_hits - base_hits,
            denominator=max(base_total, candidate_total),
            details={
                "baseline_accuracy": base_score,
                "candidate_accuracy": candidate_score,
                "group_lift": group_lift,
            },
        )

    @staticmethod
    def _group_lift(batch: EvaluationBatch) -> dict[str, float]:
        groups: dict[str, list[EvalCase]] = defaultdict(list)
        for case in batch.cases:
            groups[template_group(case)].append(case)
        out: dict[str, float] = {}
        for group, cases in groups.items():
            if len(cases) < 4:
                continue
            base_hits = base_total = candidate_hits = candidate_total = 0
            for case in cases:
                base = batch.baseline.get(case.eval_id)
                candidate = batch.candidate.get(case.eval_id)
                if base and base.record:
                    for field, expected in case.expected.items():
                        result = base.record.fields.get(field)
                        base_hits += int(
                            values_equal(
                                field, expected, result.value if result else None
                            )
                        )
                        base_total += 1
                if candidate and candidate.record:
                    for field, expected in case.expected.items():
                        result = candidate.record.fields.get(field)
                        candidate_hits += int(
                            values_equal(
                                field, expected, result.value if result else None
                            )
                        )
                        candidate_total += 1
            if base_total and candidate_total:
                out[group] = candidate_hits / candidate_total - base_hits / base_total
        return out


class ArbitrationRateEvaluator(BaseEvaluator):
    @property
    def name(self) -> str:
        return "llm_arbitration_rate"

    def evaluate(self, batch: EvaluationBatch) -> EvaluatorResult:
        base_calls = sum(
            int(bool(result.record and result.record.llm_meta.get("called")))
            for result in batch.baseline.values()
        )
        candidate_calls = sum(
            int(bool(result.record and result.record.llm_meta.get("called")))
            for result in batch.candidate.values()
        )
        denominator = len(batch.cases)
        candidate_rate = candidate_calls / denominator if denominator else None
        return EvaluatorResult(
            name=self.name,
            score=candidate_rate,
            numerator=candidate_calls,
            denominator=denominator,
            details={
                "baseline_calls": base_calls,
                "candidate_calls": candidate_calls,
                "call_delta": candidate_calls - base_calls,
            },
        )


def template_group(case: EvalCase) -> str:
    if case.template_group:
        return case.template_group
    gstin = str(case.expected.get("gstin") or "").strip().upper()
    if gstin:
        return gstin
    biller = " ".join(str(case.expected.get("billerName") or "UNKNOWN").upper().split())
    return f"NAME::{biller}"
