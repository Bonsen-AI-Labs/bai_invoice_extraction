# Repository Guidelines

## Project Structure & Architecture

Application code lives under `src/`. `src/pipeline/runner.py` orchestrates a single invoice-processing pass; `src/services/` isolates SharePoint, Excel, Cosmos DB, OCR, HTTP, and LLM integrations; `src/parsing/` and `src/extraction/` implement document interpretation; shared Pydantic types and configuration live in `src/models.py`, `src/env.py`, and `src/config.py`. Utilities are in `src/utils/`. Evaluation assets belong under `tests/evals/`.

Use `TECHNICAL_REQUIREMENTS.md` for architectural intent and field definitions. It describes the planned multi-phase system; check `CLAUDE.md` and the code for deliberate Phase 1 deviations before expanding scope.

## Build, Test, and Development Commands

- `uv sync` installs the Python 3.14 environment from `pyproject.toml` and `uv.lock`.
- `uv run python -m src.main` runs one pipeline pass; live Azure, Microsoft Graph, and OpenAI credentials are required.
- `uv run ruff check src` performs lint checks.
- `uv run pyright src` performs static type checking; keep it at zero errors.
- `uv run python -m src.utils.text` runs that module's assert-based self-check. Equivalent checks exist in `src.utils.render` and `src.extraction.client`.

## Coding Style & Naming Conventions

Use four-space indentation, explicit type annotations, and absolute imports such as `from src.models import InvoiceRecord`. Name modules, functions, and variables with `snake_case`; use `PascalCase` for classes and Pydantic models, and `UPPER_SNAKE_CASE` for constants. Keep backend SDK access behind the async clients in `src/services/`. Prefer small, deterministic functions and never invent missing financial values: emit `None` and route uncertain results for review.

## Testing Guidelines

This repository currently has no pytest suite or coverage threshold. Add focused assert-based `_demo()` checks for non-trivial pure logic and run the relevant module directly. Store evaluation datasets in `tests/evals/golden_dataset/`; do not commit real invoices, secrets, generated renders, or output workbooks.

## Commit & Pull Request Guidelines

History follows short Conventional Commit subjects, primarily `feat:` and `chore:`. Use an imperative summary, for example `feat: validate GSTIN checksum`. Pull requests should explain the user-visible change, affected pipeline stages, configuration or schema impact, and validation commands run. Link the relevant issue or technical-design section; include sample output or screenshots only when workbook or visual behaviour changes.

## Security & Configuration

Keep credentials in environment variables loaded through `src/env.py`; never commit `.env` files. Keep extraction tunables in `src/config.py`. Preserve deterministic Eval IDs, idempotent upserts, human-owned `Corr_*` columns, and DI-to-render coordinate alignment.
