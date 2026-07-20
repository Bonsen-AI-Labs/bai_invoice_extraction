"""CLI entrypoint for cached extraction evals and template promotion."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import cast

from src.template_generation import JsonTemplateStore
from tests.evals import config
from tests.evals.evaluation import EvaluationRunner
from tests.evals.evaluation.runner import EvalCommand
from tests.evals.executor import CachePolicy


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command")
    run = subcommands.add_parser(
        "run", help="run fresh baseline and template evals against live services"
    )
    run.add_argument(
        "--offline",
        action="store_true",
        help="require replay caches and make no paid service calls",
    )
    _add_limit_argument(run)
    refresh = subcommands.add_parser(
        "refresh-cache", help="refresh paid DI/LLM artefacts, then evaluate"
    )
    _add_limit_argument(refresh)
    train = subcommands.add_parser(
        "train", help="build candidate templates and evaluate held-out cases"
    )
    train.add_argument(
        "--offline",
        action="store_true",
        help="require replay caches and make no paid service calls",
    )
    _add_limit_argument(train)
    subcommands.add_parser(
        "promote", help="promote candidate templates after a passing eval"
    )
    return parser


def _add_limit_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--limit",
        type=_positive_int,
        help="evaluate only the first N executable labelled cases",
    )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("limit must be at least 1")
    return parsed


async def _run(
    command: EvalCommand, *, offline: bool = False, case_limit: int | None = None
) -> int:
    cache_policy = _cache_policy(offline=offline)
    report = await EvaluationRunner(cache_policy=cache_policy).run(
        train_templates=command == "train",
        case_limit=case_limit,
        command=command,
    )
    print(
        json.dumps(
            {
                "status": report.status,
                "run_id": report.run.run_id,
                "report_path": report.run.report_path,
                "total_rows": report.corpus.total_rows,
                "available_cases": report.run.available_case_count,
                "requested_case_limit": report.run.requested_case_limit,
                "executed_cases": report.run.selected_case_count,
                "skipped_rows": report.corpus.skipped_rows,
                "eligible_template_groups": report.eligible_template_groups,
                "summary": report.summary.model_dump(),
                "metrics": [metric.model_dump() for metric in report.metrics],
                "error_count": len(report.errors),
                "errors": report.errors[:5],
            },
            indent=2,
        )
    )
    if command == "refresh-cache":
        return 1 if report.status == "FAIL" else 0
    return {"PASS": 0, "FAIL": 1, "INSUFFICIENT_CORPUS": 2}[report.status]


def _cache_policy(*, offline: bool) -> CachePolicy:
    return "replay" if offline else "refresh"


def _promote() -> int:
    if not config.LATEST_REPORT.exists():
        raise SystemExit("no eval report found; run the evals first")
    report = json.loads(config.LATEST_REPORT.read_text(encoding="utf-8"))
    if report.get("status") != "PASS" or not report.get("eligible_template_groups"):
        raise SystemExit("candidate promotion requires a passing eligible eval run")
    JsonTemplateStore().promote()
    print("candidate templates promoted")
    return 0


def main() -> int:
    args = _parser().parse_args()
    command = cast(EvalCommand, args.command or "run")
    if command == "promote":
        return _promote()
    return asyncio.run(
        _run(
            command,
            offline=getattr(args, "offline", False),
            case_limit=getattr(args, "limit", None),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
