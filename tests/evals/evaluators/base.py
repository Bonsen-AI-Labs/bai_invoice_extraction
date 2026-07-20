from abc import ABC, abstractmethod

from tests.evals.models import EvaluationBatch, EvaluatorResult


class BaseEvaluator(ABC):
    """One evaluator owns exactly one measurable criterion."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def evaluate(self, batch: EvaluationBatch) -> EvaluatorResult: ...
