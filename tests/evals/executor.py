"""Execute extraction against deterministic DI and LLM replay artefacts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Literal

from tqdm import tqdm

from src.env import Settings
from src.extraction.client import Extractor
from src.models import OCRSnapshot
from src.parsing.client import parse, segment
from src.services.llm import LLMClient
from src.services.ocr import DIResult, OCRClient
from src.template_generation import JsonTemplateStore, TemplateEngine
from tests.evals import config
from tests.evals.models import EvalCase, ExecutionResult

CachePolicy = Literal["replay", "missing", "refresh"]


class EvalExecutor:
    def __init__(
        self,
        *,
        cache_policy: CachePolicy = "missing",
        cache_dir: Path = config.CACHE_DIR,
        active_template_path: Path | None = None,
    ) -> None:
        self._cache_policy = cache_policy
        self._cache_dir = cache_dir
        self._active_template_path = active_template_path
        self._refreshed_di_paths: set[Path] = set()

    async def execute_many(
        self,
        cases: list[EvalCase],
        *,
        template_enabled: bool,
        progress_label: str | None = None,
    ) -> dict[str, ExecutionResult]:
        live_clients = _LiveClients() if self._cache_policy != "replay" else None
        results: dict[str, ExecutionResult] = {}
        succeeded = failed = 0
        progress_enabled = progress_label is not None
        try:
            with (
                tqdm(
                    total=len(cases),
                    desc=progress_label,
                    unit="invoice",
                    dynamic_ncols=True,
                    disable=not progress_enabled,
                    position=0,
                ) as progress,
                tqdm(
                    total=4,
                    desc="Current invoice",
                    unit="stage",
                    dynamic_ncols=True,
                    disable=not progress_enabled,
                    leave=False,
                    position=1,
                ) as invoice_progress,
            ):
                for case in cases:
                    invoice_progress.reset(total=4)
                    completed_stages = 0

                    def on_stage(stage: str, completed: int) -> None:
                        nonlocal completed_stages
                        invoice_progress.set_description_str(f"Current: {stage}")
                        if completed > completed_stages:
                            invoice_progress.update(completed - completed_stages)
                            completed_stages = completed
                        invoice_progress.refresh()

                    result = await self._execute(
                        case,
                        template_enabled=template_enabled,
                        live_clients=live_clients,
                        on_stage=on_stage,
                    )
                    results[case.eval_id] = result
                    failed += int(result.error is not None)
                    succeeded += int(result.error is None)
                    progress.set_postfix(ok=succeeded, failed=failed)
                    progress.update()
        finally:
            if live_clients:
                await live_clients.aclose()
        return results

    async def _execute(
        self,
        case: EvalCase,
        *,
        template_enabled: bool,
        live_clients: _LiveClients | None,
        on_stage: Callable[[str, int], None] | None = None,
    ) -> ExecutionResult:
        started_at = datetime.now(timezone.utc).isoformat()
        started = perf_counter()
        content_hash: str | None = None
        di_source: Literal["live", "replay"] | None = None
        di_cost_usd = 0.0
        try:
            _report_stage(on_stage, "load", 0)
            pdf = case.pdf_path.read_bytes()
            content_hash = hashlib.sha256(pdf).hexdigest()
            cache = self._cache_dir / content_hash
            di_path = cache / "di.json"
            fetch_di = self._should_fetch_di(di_path)
            di_source = "live" if fetch_di else "replay"
            _report_stage(on_stage, "OCR/fetch" if fetch_di else "OCR/replay", 1)
            di = await self._di_result(pdf, di_path, live_clients)
            di_cost_usd = di.cost_usd if fetch_di else 0.0
            _report_stage(on_stage, "parse", 2)
            parsed = parse(di)
            invoices = segment(parsed)
            if len(invoices) != 1:
                raise ValueError(f"expected one logical invoice, found {len(invoices)}")
            _report_stage(on_stage, "extract", 3)
            llm_path = cache / (
                "llm-template.json" if template_enabled else "llm-baseline.json"
            )
            llm_source = self._llm_source(llm_path)
            llm = self._llm(llm_path, live_clients)
            templates = None
            if template_enabled:
                store = JsonTemplateStore(
                    active_path=self._active_template_path
                    or Path("data/templates/active.json")
                )
                templates = TemplateEngine(store)
            record = await Extractor(llm, templates).extract(
                invoices[0], pdf, case.eval_id, str(case.pdf_path)
            )
            _report_stage(on_stage, "complete", 4)
            return ExecutionResult(
                eval_id=case.eval_id,
                record=record,
                template_enabled=template_enabled,
                source_path=str(case.pdf_path),
                content_sha256=content_hash,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                duration_seconds=round(perf_counter() - started, 6),
                di_source=di_source,
                llm_source=(
                    llm_source if bool(record.llm_meta.get("called")) else None
                ),
                di_cost_usd=di_cost_usd,
                llm_cost_usd=(
                    float(record.llm_meta.get("cost_usd", 0.0))
                    if llm_source == "live"
                    else 0.0
                ),
            )
        except Exception as error:
            _report_stage(on_stage, "failed", 4)
            return ExecutionResult(
                eval_id=case.eval_id,
                error=repr(error),
                template_enabled=template_enabled,
                source_path=str(case.pdf_path),
                content_sha256=content_hash,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                duration_seconds=round(perf_counter() - started, 6),
                di_source=di_source,
                di_cost_usd=di_cost_usd,
            )

    async def _di_result(
        self, pdf: bytes, path: Path, live_clients: _LiveClients | None
    ) -> DIResult:
        if self._should_fetch_di(path):
            if live_clients is None:
                raise RuntimeError("live DI client is unavailable")
            result = await live_clients.ocr().analyze_invoice(pdf)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                result.to_snapshot().model_dump_json(indent=2), encoding="utf-8"
            )
            self._refreshed_di_paths.add(path)
            return result
        if not path.exists():
            raise FileNotFoundError(
                f"missing DI replay cache {path}; rerun without --offline"
            )
        snapshot = OCRSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
        return DIResult.from_snapshot(snapshot)

    def _should_fetch_di(self, path: Path) -> bool:
        if self._cache_policy == "missing":
            return not path.exists()
        if self._cache_policy == "refresh":
            return path not in self._refreshed_di_paths
        return False

    def _llm(self, path: Path, live_clients: _LiveClients | None):
        if self._cache_policy == "replay":
            return _ReplayLLM(path)
        if live_clients is None:
            raise RuntimeError("live LLM client is unavailable")
        if self._cache_policy == "refresh":
            return _RecordingLLM(live_clients.llm(), path)
        return _MissingCacheLLM(live_clients, path)

    def _llm_source(self, path: Path) -> Literal["live", "replay"]:
        if self._cache_policy == "refresh":
            return "live"
        if self._cache_policy == "missing" and not path.exists():
            return "live"
        return "replay"


def _report_stage(
    callback: Callable[[str, int], None] | None, stage: str, completed: int
) -> None:
    if callback:
        callback(stage, completed)


class _ReplayLLM:
    def __init__(self, path: Path) -> None:
        self._path = path

    async def arbitrate(self, disputes: list[dict[str, Any]]) -> dict[str, Any]:
        signature = _dispute_signature(disputes)
        if not self._path.exists():
            raise FileNotFoundError(
                f"missing LLM replay cache {self._path}; rerun without --offline"
            )
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if payload.get("request_signature") != signature:
            raise ValueError(f"stale LLM replay cache: {self._path}")
        return payload["response"]


class _RecordingLLM:
    def __init__(self, live: LLMClient, path: Path) -> None:
        self._live = live
        self._path = path

    async def arbitrate(self, disputes: list[dict[str, Any]]) -> dict[str, Any]:
        response = await self._live.arbitrate(disputes)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(
                {
                    "request_signature": _dispute_signature(disputes),
                    "response": response,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return response


class _MissingCacheLLM:
    """Replay an existing response, or record one live response on a cache miss."""

    def __init__(self, live_clients: _LiveClients, path: Path) -> None:
        self._live_clients = live_clients
        self._path = path

    async def arbitrate(self, disputes: list[dict[str, Any]]) -> dict[str, Any]:
        if self._path.exists():
            return await _ReplayLLM(self._path).arbitrate(disputes)
        return await _RecordingLLM(self._live_clients.llm(), self._path).arbitrate(
            disputes
        )


class _LiveClients:
    """Create paid-service clients lazily, only when a cache fetch is required."""

    def __init__(self) -> None:
        self._settings: Settings | None = None
        self._ocr: OCRClient | None = None
        self._llm: LLMClient | None = None

    def _get_settings(self) -> Settings:
        if self._settings is None:
            self._settings = Settings()  # type: ignore[call-arg]
        return self._settings

    def ocr(self) -> OCRClient:
        if self._ocr is None:
            self._ocr = OCRClient(self._get_settings())
        return self._ocr

    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(self._get_settings())
        return self._llm

    async def aclose(self) -> None:
        if self._ocr:
            await self._ocr.aclose()


def _dispute_signature(disputes: list[dict[str, Any]]) -> str:
    safe = []
    for dispute in disputes:
        safe.append(
            {
                "field": dispute.get("field"),
                "definition": dispute.get("definition"),
                "candidates": dispute.get("candidates", []),
                "note": dispute.get("note", ""),
                "crop_hashes": [
                    hashlib.sha256(crop).hexdigest()
                    for crop in dispute.get("crops", [])
                ],
            }
        )
    payload = json.dumps(safe, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()
