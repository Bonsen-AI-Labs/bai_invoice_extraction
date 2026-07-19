"""Entrypoint: run one pass of the invoice extraction pipeline.

uv run python -m src.main
"""

import asyncio

from src.pipeline.runner import run


def main() -> None:
    totals = asyncio.run(run())
    print(f"run complete: {totals}")


if __name__ == "__main__":
    main()
