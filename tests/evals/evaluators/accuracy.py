from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

from src import config as app_config
from src.utils.text import parse_amount, parse_date
from tests.evals.evaluators.base import BaseEvaluator
from tests.evals.models import EvaluationBatch, EvaluatorResult


def values_equal(field: str, expected: Any, actual: Any) -> bool:
    if expected in (None, ""):
        return actual in (None, "")
    if actual in (None, ""):
        return False
    if field in app_config.AMOUNT_FIELDS:
        left, right = parse_amount(str(expected)), parse_amount(str(actual))
        return (
            left is not None and right is not None and round(left, 2) == round(right, 2)
        )
    if field in app_config.PERCENT_FIELDS:
        left, right = parse_amount(str(expected)), parse_amount(str(actual))
        return left is not None and right is not None and abs(left - right) <= 0.01
    if field == "date":
        return _date_value(expected) == _date_value(actual)
    if field == "gstin":
        return (
            re.sub(r"\s+", "", str(expected)).upper()
            == re.sub(r"\s+", "", str(actual)).upper()
        )
    if field == "billerPhone":
        return re.sub(r"\D", "", str(expected)) == re.sub(r"\D", "", str(actual))
    if field == "currency":
        return str(expected).strip().upper() == str(actual).strip().upper()
    if field == "totalLineItems":
        try:
            return int(float(expected)) == int(float(actual))
        except TypeError, ValueError:
            return False
    return _normal_text(expected) == _normal_text(actual)


def field_accuracy(
    batch: EvaluationBatch, candidate: bool
) -> tuple[int, int, dict[str, tuple[int, int]]]:
    results = batch.candidate if candidate else batch.baseline
    correct = total = 0
    by_field: dict[str, tuple[int, int]] = {}
    for case in batch.cases:
        execution = results.get(case.eval_id)
        if not execution or not execution.record:
            continue
        for field, expected in case.expected.items():
            actual_result = execution.record.fields.get(field)
            actual = actual_result.value if actual_result else None
            hit = values_equal(field, expected, actual)
            field_correct, field_total = by_field.get(field, (0, 0))
            by_field[field] = (field_correct + int(hit), field_total + 1)
            correct += int(hit)
            total += 1
    return correct, total, by_field


class FieldAccuracyEvaluator(BaseEvaluator):
    @property
    def name(self) -> str:
        return "field_accuracy"

    def evaluate(self, batch: EvaluationBatch) -> EvaluatorResult:
        correct, total, by_field = field_accuracy(batch, candidate=True)
        return EvaluatorResult(
            name=self.name,
            score=correct / total if total else None,
            numerator=correct,
            denominator=total,
            details={
                field: hits / count if count else None
                for field, (hits, count) in by_field.items()
            },
        )


def _normal_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(text.split())


def _date_value(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return (
            (datetime(1899, 12, 30) + timedelta(days=float(value))).date().isoformat()
        )
    raw = str(value).strip().replace(",", "").replace("0CT", "OCT")
    parsed = parse_date(raw)
    if parsed:
        return parsed
    for date_format in ("%d %b %Y", "%d-%b %Y", "%d-%b-%y"):
        try:
            return datetime.strptime(raw, date_format).date().isoformat()
        except ValueError:
            continue
    return None
