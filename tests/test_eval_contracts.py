"""Pytest discovery wrappers for the executable eval contract suite."""

from tests.evals.contracts import (
    check_cache_signature,
    check_corpus_contract,
    check_di_snapshot_round_trip,
    check_pattern_induction,
    check_report_redacts_labels,
    check_template_learning_and_promotion,
    check_welford,
)


def test_welford() -> None:
    check_welford()


def test_pattern_induction() -> None:
    check_pattern_induction()


def test_template_learning_and_promotion() -> None:
    check_template_learning_and_promotion()


def test_corpus_contract() -> None:
    check_corpus_contract()


def test_cache_signature() -> None:
    check_cache_signature()


def test_report_redacts_labels() -> None:
    check_report_redacts_labels()


def test_di_snapshot_round_trip() -> None:
    check_di_snapshot_round_trip()
