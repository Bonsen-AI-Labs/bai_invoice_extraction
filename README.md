# bai_invoice_extraction

Internal invoice extraction tool for finance. Extracts a fixed 18-field header
schema from Indian GST invoice PDFs in a SharePoint folder, writes each row to a
master Excel workbook, and records execution history in Cosmos.

## Status

Phase 1 core pipeline **plus** the per-vendor template system (Layer L6) and an
offline eval + learning harness. Per run:

`SharePoint list → SHA-256 gate → download → Azure DI → parse + segment →
extract (L1/L2/L3/L6 → fuse → validate → vision-LLM arbitration) → soft-dup vs
Excel → Excel upsert → Cosmos history`

Where things live:
- **Master Excel workbook** — the extracted values (de-facto store of record).
- **Cosmos** — execution history only (SHA-256 dedup gate + run ledger). No
  invoice/template documents.
- **`data/templates/*.json`** — vendor-layout templates (local JSON, not Cosmos).

## Commands

Managed with [uv](https://docs.astral.sh/uv/); Python 3.14.

Common workflows are available through [Task](https://taskfile.dev/):

```bash
task lint    # ruff format + ruff check --fix + pyright
task pytest  # deterministic contracts through pytest
task evals   # fresh live baseline + template evals
task train   # build candidates and evaluate with fresh live results
```

Run `task --list` to see the available workflows. Eval and training commands
show an overall invoice bar plus a per-invoice stage bar
(`load → OCR/fetch or replay → parse → extract`) with running success/failure
counts. The active stage remains visible while a live OCR or LLM request is in
progress. The normal SharePoint pipeline currently prints final totals rather
than a progress bar.

The underlying commands remain available directly:

```bash
uv sync                          # install/resolve dependencies
uv run python -m src.main        # run one pipeline pass (needs live creds + env)
uv run ruff check src            # lint  (must stay clean)
uv run pyright src               # type-check (must stay clean: 0 errors)
```

### Evals & template promotion

```bash
uv run python -m tests.evals.main run            # refresh live baseline + template results
uv run python -m tests.evals.main run --limit 50 # first 50 executable labelled cases
uv run python -m tests.evals.main run --offline  # replay only; fail on cache misses
uv run python -m tests.evals.main refresh-cache  # explicit alias for a fresh live eval
uv run python -m tests.evals.main train          # train and evaluate with live results
uv run python -m tests.evals.main train --offline # train using replay data only
uv run python -m tests.evals.main promote        # promote candidates after a passing eval
```

The corpus is the golden dataset at
`tests/evals/golden_dataset/invoice_extraction_evals_v1.xlsx` (predicted versus
corrected per field). `task evals` and `task train` invoke live DI/LLM services
and replace their replay artefacts on every run, so they can incur costs. Use
`--offline` for deterministic CI with no paid calls. `refresh-cache` is retained
as an explicit alias for a fresh live evaluation.

Each run writes `out/evals/latest.json` and an immutable timestamped report in
`out/evals/<run-id>.json`. Reports contain run/configuration metadata, corpus
issues, expected values, complete baseline and template extraction records,
field-level comparisons, costs, timings, errors, and overall/per-column accuracy
percentages. The current golden workbook has 190 IDs but only 30 executable
labelled rows; a larger `--limit` records the request but cannot manufacture
missing source paths or expected values.

### Tests and auxiliary self-checks

`task pytest` is the required deterministic test suite. Non-trivial pure logic
also carries lightweight assert-based `_demo()` checks for targeted debugging:

```bash
uv run python -m src.utils.text          # GSTIN checksum, fuzzy anchors, value parsing
uv run python -m src.utils.render        # polygon→pixel crop math
uv run python -m src.extraction.client   # combinatorial V-ARITH winner selection
```

## Templates (L6)

Extraction reads only **promoted** templates from `data/templates/active.json`;
a cold start (no file) is fully supported — templates only sharpen, never gate.

Learning is quarantined. Offline (`train`) and opt-in live learning
(`TEMPLATE_LIVE_LEARNING=true`) write candidates to
`data/templates/candidate.json` — never touching active priors. Candidates must
pass held-out evals before `promote` copies them into `active.json`. Only
*confirmed* extractions (V-ARITH PASS, or validation-consistent + high-confidence
LLM) ever teach a template. Each vendor-layout group needs at least three
confirmed training invoices and one held-out invoice before it is eligible for
training and promotion.

## Local mode

Set `ENVIRONMENT=local`. After a normal pipeline run, `src.main` also runs the
annotated-image visualiser (`src/utils/visualize.py`), which reads PDFs straight
from `data/invoices/`, runs OCR → parse → segment → extract, and writes
`out/<file>-<evalid>-p<n>.png` with each extracted field boxed and labelled
`field (confidence)`. The visualiser is read-only — it writes nothing to
SharePoint, Excel, or Cosmos.

## Configuration

- **`.env`** (loaded by `src/env.py::Settings`) — secrets/connection info: Graph
  app creds, Cosmos, DI, SharePoint folder + master Excel URLs, `OPENAI_API_KEY`,
  plus `LOG_LEVEL`, `ENVIRONMENT` (`dev`|`local`), `TEMPLATE_LIVE_LEARNING`.
- **`src/config.py`** — extraction tunables, template thresholds, anchor lexicon.
- The OpenAI vision model id is `OPENAI_VISION_MODEL` in `config.py`
  (default `gpt-5-mini`; override via the env var of the same name).

## Not yet built (later phases)

Blob staging + resume-from-stage (a retry re-runs the whole file and re-pays DI);
Cosmos `invoices`/`runs`/`templates` containers (history only today); cron
scheduler + single-flight lock; L4 positional prior and the full L5 table engine;
confidence calibration; a dedicated human-validation UI.
