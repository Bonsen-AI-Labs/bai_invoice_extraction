# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Two documents, two scopes

- **`TECHNICAL_REQUIREMENTS.md`** is the full multi-phase design (SharePoint → DI →
  6-layer extraction → templates → Cosmos + workbook, with Blob staging, cron, and a
  learning loop). Read it for the *why* behind a mechanism; its `§` sections are cited
  throughout the code.
- **This file describes what is actually built** — Phase 1 plus the Phase 2
  template engine and eval harness. It deliberately implements a narrower slice
  than the design, and in places *contradicts* it (see "Deviations from the design
  doc"). When the code and the design doc disagree, the code is authoritative and
  the divergence is intentional.

## What is built

Extract a fixed 18-field header schema from Indian GST invoice PDFs in a SharePoint
folder, write each row to a master Excel workbook, and record execution history in
Cosmos. One manual pass per run:

`SharePoint list → SHA-256 gate → download → Azure DI → parse + segment → extract
(L1/L2/L3 + matched L6 priors → fuse → validate → vision-LLM arbitration) →
soft-dup vs Excel → Excel upsert → Cosmos history`

Entrypoint: `uv run python -m src.main` (`src/pipeline/runner.py::run`). When
`ENVIRONMENT=local`, `src/main.py` runs the read-only annotated-image visualiser
(`src/utils/visualize.py`) after the pipeline pass: it reads PDFs straight from
`data/invoices/`, re-runs OCR → parse → segment → extract, and writes
`out/<file>-<evalid>-p<n>.png` with each field boxed and labelled
`field (confidence)`. The visualiser writes nothing to SharePoint/Excel/Cosmos.

Phase 2 adds the local JSON-backed vendor/layout template engine (L6) and the
cached eval harness under `tests/evals/`. Active templates are extraction inputs;
all learning writes quarantined candidates which require a passing held-out eval
before promotion. Full Cosmos invoice/template persistence, Blob staging, cron,
and the validation UI remain out of scope.

## Commands

Managed with [uv](https://docs.astral.sh/uv/); Python 3.14.

```bash
uv sync                          # install/resolve dependencies
uv run python -m src.main        # run one pipeline pass (needs live creds + env)
uv run ruff check src tests/evals main.py  # lint (must stay clean)
uv run pyright src tests/evals main.py     # type-check (0 errors)
uv run python -m tests.evals.main run   # deterministic cached eval replay
```

No pytest suite. Non-trivial pure logic carries an assert-based `_demo()` under
`if __name__ == "__main__":` — run them directly:

```bash
uv run python -m src.utils.text          # GSTIN checksum, fuzzy anchors, value parsing
uv run python -m src.utils.render        # polygon→pixel crop math
uv run python -m src.extraction.client   # combinatorial V-ARITH winner selection
```

Add a self-check like these when you write new non-trivial logic; don't add a test framework.

## Architecture: client-based isolation

The core rule of this repo. Each backend has exactly one client under `src/services/`
that exposes a public async surface; **pipeline / parsing / extraction code calls those
methods and never touches a backend SDK or HTTP directly.**

- `services/http.py` — async httpx + MSAL. Owns Microsoft Graph token acquisition/caching/retry. `graph()`, `graph_json()`, `download()`.
- `services/sharepoint.py` — Graph file listing/download (via `HTTPClient`). Resolves the folder share URL to a drive/item.
- `services/excel.py` — Graph **workbook table** API (via `HTTPClient`). `read_rows()`, `find_row()`, `upsert_row()`, `add_failure()`. Table-object ops so writes survive user sorting.
- `services/ocr.py` — Azure Document Intelligence SDK. `analyze_invoice()` runs `prebuilt-invoice` + `prebuilt-layout`, returns a `DIResult`.
- `services/cosmos.py` — Cosmos SDK, **single history container only**. `is_processed()` (dedup gate), `record_history()`, `find_identity()`.
- `services/llm.py` — OpenAI vision SDK. `arbitrate()` — strict-JSON, one call per invoice. Model is `OPENAI_VISION_MODEL` (default `gpt-5-mini`, which only allows the default temperature — determinism leans on the JSON schema, not `temperature=0`).

The engine:
- `parsing/client.py` — `parse(DIResult) → ParsedDocument` (grouped blocks, §5.4) and `segment(...) → [LogicalInvoice]` (§5.5, uses DI document boundaries; raises `SegmentationError` when ambiguous → pipeline parks the file).
- `extraction/client.py` — `Extractor.extract(inv, pdf, eval_id, path) → InvoiceRecord`. Candidate generation L1/L2/L3 plus optional matched L6 priors → fusion → validation → conditional LLM arbitration.
- `template_generation/` — fingerprint matching, L6 prior injection, Welford learning, and atomic active/candidate JSON storage.
- `tests/evals/` — Excel golden-corpus reader, paid-service replay cache, criterion-specific evaluators, training split, non-regression gate, and promotion CLI.
- `models.py` — Pydantic shapes (`Candidate` §7.2, `FieldResult`, `InvoiceRecord`, `HistoryDoc`).
- `config.py` — Phase-1 tunable constants + anchor lexicon (§18.1/§18.2).

## Load-bearing invariants (span files — don't break in one module)

- **Never invent a value.** No candidate → field is `null` + `NeedsReview`. Deterministic validation (§8) outranks the LLM; the LLM may not override a checksum-valid GSTIN or newly break a satisfied V-ARITH (§9.5, enforced in `extraction/client.py::_arbitrate`).
- **Eval ID is deterministic** — `EVL-<sha256(bytes)[:12]>-<NN>` per logical invoice (§11.4). This is what makes re-runs idempotent: same bytes → same Eval ID → Excel `upsert_row` and Cosmos upsert overwrite in place. Never blind-insert.
- **Coordinate space, not image space.** DI polygons for PDF input are in **inches**, origin top-left (§6.3). Crops (`utils/render.py`) are cut by pixel box = inches × DPI; the raster is never rotated (§5.3).
- **Human `Corr_*` columns are off-limits.** The pipeline writes only Identity/Extracted/Operational columns (`InvoiceRecord.workbook_row()`); the (predicted, corrected) pair keyed by Eval ID is the future eval/training signal (§12.2).
- **Absolute imports only** — `from src.x import ...`, never relative (`from ..x`). Consistent across the tree.
- **`ruff` and `pyright` must both stay clean.** Prefer real types over `cast`/`# type: ignore`; the one standing ignore is `Settings()  # type: ignore[call-arg]` (pydantic-settings loads fields from env).

## Deviations from the design doc

These are intentional and will change in later phases — do not "fix" them toward the design doc without a scope decision:

- **Cosmos stores execution history only** (single `COSMOS_HISTORY_CONTAINER`). The design's "Cosmos is source of truth, workbook is a regenerable view" does **not** hold: the **master Excel workbook is the de-facto store of extracted values**. No `invoices`/`templates` containers.
- **No Blob staging / no resume.** The design's "pay for DI once, persist-before-parse, resume-from-stage" is out — a retry re-runs the whole file (renders are in-memory only).
- **No Cosmos template persistence.** L6 templates use ignored local JSON files;
  active and candidate stores are deliberately separated so live predictions can
  never teach directly into active extraction.
- **Tunable constants live in `src/config.py`**, not the Cosmos `_GLOBAL/config` doc.
- **No cron/lock** — a single manual pass. Biller-vs-payee GSTIN role resolution and rate-derived tax amounts are heuristic; such simplifications are marked with `ponytail:` comments.

## Config surfaces (don't conflate)

- **`src/env.py`** (`Settings(BaseSettings)`, loaded from `.env`) — secrets/connection info plus a few behavior flags: Graph app creds (`APPLICATION_CLIENT_ID/SECRET/TENANT_ID`), Cosmos, DI, SharePoint folder + master Excel URLs, `OPENAI_API_KEY`, and `LOG_LEVEL` / `ENVIRONMENT` (`dev`|`local`) / `TEMPLATE_LIVE_LEARNING` (default false — gates quarantined live learning in the runner).
- **`src/config.py`** — extraction tunables and the anchor lexicon.
- The OpenAI vision model id is `OPENAI_VISION_MODEL` in `config.py` (override via the env var of the same name); confirm the exact id for the account before a live run.
