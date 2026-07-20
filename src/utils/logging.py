"""OpenTelemetry observability: structured logs + traces to stdout.

Call `setup_observability(settings)` once at process start (main.py / visualize.py /
evals). After that every stdlib `logging` call is bridged to a structured OTel
LogRecord printed to stdout, and `get_tracer(...)` spans print as JSON too. Attach
per-record fields with the stdlib `extra=` kwarg:

    log = get_logger(__name__)
    log.info("processed", extra={"eval_id": eid, "file": name})

Run the self-check: `uv run python -m src.utils.logging`
"""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

# SDKs that log every HTTP request at INFO — muted to WARNING.
_NOISY = (
    "azure",
    "azure.core.pipeline.policies.http_logging_policy",
    "httpx",
    "openai",
)

_configured = False


def setup_observability(settings) -> None:
    """Idempotent: wire OTel console log + trace exporters and the stdlib bridge."""
    global _configured
    if _configured:
        return

    resource = Resource.create(
        {"service.name": "bai-invoice-extraction", "environment": settings.ENVIRONMENT}
    )

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(ConsoleLogExporter())
    )
    set_logger_provider(logger_provider)

    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL)
    root.addHandler(LoggingHandler(logger_provider=logger_provider))
    for noisy in _NOISY:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def get_tracer(name: str):
    return trace.get_tracer(name)


def _demo() -> None:
    global _configured

    class _S:
        LOG_LEVEL = "DEBUG"
        ENVIRONMENT = "local"

    _configured = False
    setup_observability(_S())
    setup_observability(_S())  # second call is a no-op

    assert trace.get_tracer_provider() is not None
    root = logging.getLogger()
    assert any(isinstance(h, LoggingHandler) for h in root.handlers)
    assert root.level == logging.DEBUG

    with get_tracer(__name__).start_as_current_span("selfcheck"):
        get_logger(__name__).info("hello", extra={"eval_id": "EVL-demo-00"})

    print("logging self-check ok")


if __name__ == "__main__":
    _demo()
