# Repository Guidelines

## Project Structure & Module Organisation

Application code lives under `src/`. `src/pipeline/runner.py` orchestrates processing; `src/services/` isolates external systems; `src/parsing/` and `src/extraction/` interpret documents. Shared types and configuration live in `src/models.py`, `src/env.py`, and `src/config.py`. Utilities are in `src/utils/`; eval assets are under `tests/evals/`.

Layer L6 template learning lives in `src/template_generation/`. Runtime templates are under `data/templates/`; golden expectations are in `tests/evals/golden_dataset/`. PDFs and replay caches remain under ignored `data/` paths. Use `TECHNICAL_REQUIREMENTS.md` for architectural intent and field definitions.

## Build, Test, and Development Commands

- `uv sync` installs the locked Python 3.14 environment.
- `task lint` runs `ruff format`, `ruff check --fix`, and Pyright.
- `task pytest` runs the deterministic test suite.
- `task evals` refreshes live DI/LLM results, then compares baseline and templates.
- `task train` learns candidates and evaluates them with fresh live results.
- `uv run python -m src.main` runs a live SharePoint pass; Azure, Graph, and OpenAI credentials are required.

Eval and training runs display batch and per-invoice progress. They invoke paid DI and LLM services by default and persist responses. Use `run --offline` in CI. Detailed reports under `out/evals/` contain invoice values and provenance; keep that ignored directory private.

## Coding Style & Naming Conventions

Use four-space indentation, explicit type annotations, and absolute imports such as `from src.models import InvoiceRecord`. Name modules, functions, and variables with `snake_case`; use `PascalCase` for classes and Pydantic models, and `UPPER_SNAKE_CASE` for constants. Keep backend SDK access behind the async clients in `src/services/`. Prefer small, deterministic functions and never invent missing financial values: emit `None` and route uncertain results for review.

## Testing Guidelines

Add pytest contracts for deterministic behaviour and keep them independent of live services. Name files `test_*.py` and tests `test_<behaviour>()`. Template learning needs three confirmed training invoices and one held-out invoice per vendor-layout group. Never promote `candidate.json` without passing held-out evals. There is no numeric coverage threshold; cover changed branches and failure modes.

## Commit & Pull Request Guidelines

History uses Conventional Commit subjects, primarily `feat:` and `chore:`. Use an imperative summary, for example `feat: validate GSTIN checksum`. Pull requests should explain the user-visible change, affected pipeline stages, configuration or schema impact, and commands run. Link the issue or design section; include screenshots when output changes.

## Security & Configuration

Keep credentials in environment variables loaded through `src/env.py`; never commit `.env` files. Keep extraction tunables in `src/config.py`. Preserve deterministic Eval IDs, idempotent upserts, human-owned `Corr_*` columns, and DI-to-render coordinate alignment.
