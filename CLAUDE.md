# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Early-stage scaffold. Most `src/` modules are intentionally empty stubs. The
full design is in **`TECHNICAL_REQUIREMENTS.md`** — that document is
authoritative; read it before implementing anything non-trivial. This file only
captures the cross-cutting invariants that are easy to violate while working in
a single module.

The root `main.py` is a hello-world entrypoint. `src/main.py` is copy-pasted
Azure Document Intelligence quickstart sample code — reference material, not the
real implementation.

## What this is

A Python cron pipeline that extracts a fixed 18-field header schema from scanned
Indian GST invoice PDFs dropped in SharePoint, writes results to a master Excel
workbook, and keeps Cosmos DB as the system of record. Flow (design §3.1):

`SharePoint → hash-dedup → Blob staging → PyMuPDF render → Azure Document
Intelligence → segment into logical invoices → template lookup → 6-layer
extraction → deterministic validation → conditional vision-LLM arbitration →
Cosmos + workbook → template learning`

Build order is staged — see design §16.1. Start with the L1-only skeleton
(ingest → DI → Cosmos → workbook append); layers, templates, and the learning
loop come later.

## Load-bearing invariants (span multiple files — don't break in isolation)

- **Cosmos is the source of truth; the workbook is a regenerable view.** Never
  treat the workbook as authoritative.
- **Pay for DI once.** Write both raw DI JSON responses to Blob *before* parsing
  (§6.1). Retries resume from the persisted stage and re-read DI from Blob —
  never re-purchase it. Per-file state machine in §3.2.
- **Eval ID is deterministic**: `EVL-<sha256(bytes)[:12]>-<NN>` (§11.4). This is
  what makes the pipeline idempotent — reprocessing upserts the same Cosmos
  docs and workbook rows. Everything is upsert, never blind insert.
- **Never invent field values.** No candidate → null + NeedsReview. Validation
  outranks the LLM; the LLM outranks nothing that breaks a satisfied check
  (§8.8, §9.5).
- **Templates are priors, never ground truth** (§7.6, §10.7). They only update
  from *confirmed* extractions (V-ARITH pass / human-approved / LLM-high-conf +
  consistent). Unvalidated guesses must never teach a template.
- **Coordinate space, not image space.** DI polygons for PDF input are in
  **inches**, origin top-left (§6.3). Deskew/rotation is done on coordinates;
  the render used for LLM crops is never geometrically altered (§5.3). Every
  stored polygon keeps `(page, unit, pageWidth, pageHeight)` alongside it.
- **Human corrections live in separate `Corr_*` columns.** The cron writes
  Extracted/Operational and never touches human columns; the (predicted,
  corrected) pair is eval data + template signal — never overwrite it (§12.2).
- **All tunable constants live in the versioned `_GLOBAL/config` Cosmos doc**
  (§18.1), not scattered literals. Each invoice records the config versions it
  used.

## Commands

Managed with [uv](https://docs.astral.sh/uv/) (see `uv.lock`). Requires Python 3.14.

```bash
uv sync                 # install/resolve dependencies
uv run python main.py   # run root entrypoint
```

No test suite or linter is configured yet.

## Module map (`src/` → design section)

- `services/sharepoint.py` — Graph API listing/download + workbook table writes (§4, §12).
- `services/ocr.py` — Azure Document Intelligence pass, `prebuilt-invoice` + `prebuilt-layout` (§6).
- `services/cosmos.py` — three containers: `invoices` (pk `/vendorKey`), `runs` (pk `/yearMonth`), `templates` (pk `/vendorKey`, also holds `_GLOBAL/config`) (§11.1).
- `services/http.py` — shared async HTTP client; other service clients build on it.
- `parsing/client.py` — DI JSON → grouped blocks/tables (§5, §7.5).
- `extraction/client.py` — the 6-layer engine (L1–L6), fusion, validation, arbitration (§7–§9).
- `template_generation/` — fingerprinting, matching, injection (L6), learning (§10).
- `models.py` — Pydantic domain models (candidate, invoice doc, template doc — canonical shapes in §7.2, §10.4, §11.2).
- `env.py` — `Settings(BaseSettings)`; secrets from env (`DOC_INTEL_ENDPOINT`, `DOC_INTEL_KEY`). Runtime *tunables* go in the Cosmos `_GLOBAL/config` doc, not here.

## Config split

Two distinct config surfaces — don't conflate them:
- **`src/env.py` (Pydantic Settings, from env)** — secrets and connection info only. Never hardcode endpoints/keys (the Azure sample in `src/main.py` does; don't copy it).
- **Cosmos `_GLOBAL/config` (versioned)** — anchor lexicon, fusion weights, thresholds, all §18.1 constants. Changing these is auditable and gated on eval deltas (§16.2).
