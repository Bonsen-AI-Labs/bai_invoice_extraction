"""Entrypoint: run one pass of the invoice extraction pipeline.

uv run python -m src.main
"""

import asyncio

from src.env import Settings
from src.pipeline.runner import run
from src.utils.logging import get_logger, setup_observability


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    setup_observability(settings)
    totals = asyncio.run(run())
    print(f"run complete: {totals}")
    if settings.ENVIRONMENT == "local":
        try:
            from src.utils.visualize import run as run_visualizer

            asyncio.run(run_visualizer())
        except Exception:
            get_logger(__name__).exception(
                "local visualizer failed after a successful pipeline run"
            )


if __name__ == "__main__":
    main()
