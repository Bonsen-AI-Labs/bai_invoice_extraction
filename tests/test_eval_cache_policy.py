"""Behavioural tests for eval replay and live cache fallback."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from src.services.ocr import DIResult
from tests.evals.executor import EvalExecutor, _LiveClients, _MissingCacheLLM
from tests.evals.main import _cache_policy


class _AnalysisResult:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def as_dict(self) -> dict[str, Any]:
        return self._payload


class _FakeOCR:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze_invoice(self, _: bytes) -> DIResult:
        self.calls += 1
        return DIResult(
            invoice=_AnalysisResult({"documents": []}),
            layout=_AnalysisResult({"pages": [], "tables": []}),
            pages=0,
            cost_usd=0.0,
        )


class _FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def arbitrate(self, _: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls += 1
        return {"verdicts": {}, "cost_usd": 0.0, "fields": []}


class _FakeLiveClients:
    def __init__(self) -> None:
        self.ocr_client = _FakeOCR()
        self.llm_client = _FakeLLM()

    def ocr(self) -> _FakeOCR:
        return self.ocr_client

    def llm(self) -> _FakeLLM:
        return self.llm_client


def test_eval_runs_are_live_unless_offline_is_explicit() -> None:
    assert _cache_policy(offline=False) == "refresh"
    assert _cache_policy(offline=True) == "replay"


def test_missing_di_cache_is_fetched_once_then_replayed(tmp_path: Path) -> None:
    executor = EvalExecutor(cache_policy="missing", cache_dir=tmp_path)
    path = tmp_path / "invoice" / "di.json"
    clients = _FakeLiveClients()

    first = asyncio.run(executor._di_result(b"pdf", path, cast(_LiveClients, clients)))
    second = asyncio.run(executor._di_result(b"pdf", path, cast(_LiveClients, clients)))

    assert first.pages == second.pages == 0
    assert clients.ocr_client.calls == 1
    assert path.exists()


def test_offline_policy_rejects_missing_di_cache(tmp_path: Path) -> None:
    executor = EvalExecutor(cache_policy="replay", cache_dir=tmp_path)

    with pytest.raises(FileNotFoundError, match="rerun without --offline"):
        asyncio.run(executor._di_result(b"pdf", tmp_path / "di.json", None))


def test_missing_llm_cache_is_fetched_once_then_replayed(tmp_path: Path) -> None:
    path = tmp_path / "llm.json"
    clients = _FakeLiveClients()
    llm = _MissingCacheLLM(cast(_LiveClients, clients), path)
    disputes = [{"field": "billNumber", "candidates": [], "crops": []}]

    first = asyncio.run(llm.arbitrate(disputes))
    second = asyncio.run(llm.arbitrate(disputes))

    assert first == second
    assert clients.llm_client.calls == 1
    assert json.loads(path.read_text(encoding="utf-8"))["response"] == first


def test_stale_llm_cache_is_not_silently_replaced(tmp_path: Path) -> None:
    path = tmp_path / "llm.json"
    path.write_text(
        json.dumps({"request_signature": "stale", "response": {}}),
        encoding="utf-8",
    )
    clients = _FakeLiveClients()
    llm = _MissingCacheLLM(cast(_LiveClients, clients), path)

    with pytest.raises(ValueError, match="stale LLM replay cache"):
        asyncio.run(
            llm.arbitrate([{"field": "billNumber", "candidates": [], "crops": []}])
        )

    assert clients.llm_client.calls == 0
