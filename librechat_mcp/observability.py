"""
Observability setup for librechat-mcp — structlog (always on) + optional OTEL.

Logs go to stdout as JSON. OTEL is opt-in on OTEL_EXPORTER_OTLP_ENDPOINT and exports
metrics (a call counter and a duration histogram) plus one span per tool call.

Adapted from nats-mcp v0.3.0, the forge reference implementation. The reasoning
carried inline below is that repo's, retained rather than paraphrased.

**Why this file exists at all.** Until v0.2.0 `structlog.configure()` was never
called anywhere in librechat-mcp. The README's "Structured JSON logs via structlog"
was false, `LOG_LEVEL` was documented in AGENTS.md and read by no code, and the
container had produced zero application log lines in its entire deployed life. That
silence is a large part of why every tool could fail for six weeks unnoticed
(vikunja#657) — there was nowhere for the failure to show up.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager

import structlog

log = structlog.get_logger("librechat-mcp")

# Library loggers demoted to WARNING by configure_logging(). These are logger
# *prefixes* — `logging` applies the level to every child (`httpcore.http11`,
# `mcp.server.lowlevel`, `uvicorn.access`, ...) through normal propagation.
_THIRD_PARTY_LOGGERS = ("httpx", "httpcore", "mcp", "fastmcp", "uvicorn", "starlette")

_APP_LOGGER = "librechat-mcp"


def configure_logging() -> None:
    """Configure JSON logging on stdout for the `librechat-mcp` logger.

    stdout, not stderr and not a file. This process runs in a container, where a
    FileHandler is invisible to `docker logs` and to Loki and grows unbounded on a
    bind mount; log rotation is the compose layer's job (`logging: json-file` with
    max-size/max-file). A FileHandler is attached only when LOG_FILE is explicitly
    set, and the deploy fragment deliberately leaves it unset so the container needs
    no writable path at all.
    """
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    # No default path. A hardcoded default would have every run create a directory
    # and an unrotated file whether anyone wanted one.
    log_file = os.environ.get("LOG_FILE")

    shared_processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        # Renders `exc_info=True` into the record. `_tool_error` passes it for
        # exceptions nobody anticipated, and without this processor that flag is
        # accepted and silently dropped — the traceback would never appear.
        structlog.processors.format_exc_info,
    ]

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        try:
            log_dir = os.path.dirname(log_file)
            # A bare filename ("librechat.log") has no directory part, and
            # os.makedirs("") raises FileNotFoundError.
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            handlers.append(logging.FileHandler(log_file))
        except OSError as exc:
            # An unwritable path (CI runner, read-only container fs) must not crash
            # startup — stdout is already attached and is the primary sink.
            print(f"librechat-mcp: file logging disabled ({log_file}): {exc}", file=sys.stderr)

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )
    for h in handlers:
        h.setFormatter(formatter)

    # Attach to the named app logger, NOT the root logger. Clearing root's handlers
    # and setting root to LOG_LEVEL captured and JSON-rendered every third-party
    # record (httpx wire trace, fastmcp request logging, uvicorn access lines) at the
    # app's level — the untyped-noise pattern remediated in dockhand-mcp
    # (vikunja#574) and task-dispatcher (#552).
    app_logger = logging.getLogger(_APP_LOGGER)
    app_logger.handlers.clear()
    for h in handlers:
        app_logger.addHandler(h)
    app_logger.setLevel(getattr(logging, log_level, logging.INFO))
    # Do not re-emit through root, which may carry a caller's own handlers.
    app_logger.propagate = False

    for _noisy in _THIRD_PARTY_LOGGERS:
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ---------------------------------------------------------------------------
# OTEL (opt-in on OTEL_EXPORTER_OTLP_ENDPOINT)
# ---------------------------------------------------------------------------
#
# Endpoint is gRPC OTLP — port 4317, not 4318. The exporter imported below is
# `opentelemetry.exporter.otlp.proto.grpc`, and the [otel] extra pins
# opentelemetry-exporter-otlp-proto-grpc. Pointing it at 4318 (the HTTP port) fails
# silently. On forge: http://localhost:4317 on the host,
# http://signoz-otel-collector:4317 in a container.
#
# A configured-but-failing backend must be visible and must not be retried on every
# tool call. `_otel_failed` is a distinct flag rather than an overloaded None so
# "never tried" (env var unset — the intended silent path) stays distinguishable from
# "tried and failed" (warn exactly once).

_tracer = None
_tracer_provider = None
_meter_provider = None
_call_counter = None
_duration_histogram = None
_otel_failed = False


def _init_otel() -> None:
    global _tracer, _tracer_provider, _meter_provider
    global _call_counter, _duration_histogram, _otel_failed

    if _tracer is not None or _otel_failed:
        return
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return  # backend not configured — intended disabled path, stay silent

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": "librechat-mcp"})

        _tracer_provider = TracerProvider(resource=resource)
        _tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(_tracer_provider)
        _tracer = trace.get_tracer("librechat-mcp")

        # Metrics carry the latency signal, not spans. Measured on dockhand-mcp: the
        # same call cost 121s exported as a span vs 0.1s as a metric, because a
        # per-call span export blocks on the collector round trip while the metric
        # reader batches on its own interval.
        _meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))],
        )
        metrics.set_meter_provider(_meter_provider)
        meter = metrics.get_meter("librechat-mcp")
        _call_counter = meter.create_counter(
            "librechat_mcp.tool.calls",
            unit="1",
            description="librechat-mcp tool invocations, by tool and outcome",
        )
        _duration_histogram = meter.create_histogram(
            "librechat_mcp.tool.duration",
            unit="ms",
            description="librechat-mcp tool call duration",
        )
    except Exception:
        _otel_failed = True
        # exc_info is kept deliberately. OTEL_EXPORTER_OTLP_ENDPOINT is a bare URL
        # with no credential in it, and the try block spans eight imports plus two
        # exporter builds — an exception class alone would not say which failed,
        # which is the whole diagnostic question when the [otel] extra is missing.
        log.warning("otel_init_failed", exc_info=True)


def get_tracer():
    """Return the OTEL tracer, or None when OTEL is not configured or failed."""
    _init_otel()
    return _tracer


@contextmanager
def observe_tool(tool: str, **attributes: object):
    """Record one tool call: a span plus a call counter and duration histogram.

    A no-op beyond timing when OTEL is not configured, so the tools can be wrapped
    unconditionally.
    """
    _init_otel()
    started = time.perf_counter()
    outcome = "ok"
    span_cm = (
        _tracer.start_as_current_span(f"librechat_mcp.{tool}")
        if _tracer is not None
        else _null_context()
    )
    try:
        with span_cm as span:
            if span is not None:
                for key, value in attributes.items():
                    span.set_attribute(key, value)
            yield
    except Exception as exc:
        outcome = type(exc).__name__
        raise
    finally:
        duration_ms = (time.perf_counter() - started) * 1000
        labels = {"tool": tool, "outcome": outcome}
        if _call_counter is not None:
            _call_counter.add(1, labels)
        if _duration_histogram is not None:
            _duration_histogram.record(duration_ms, labels)


@contextmanager
def _null_context():
    yield None


def instrument(tool: str) -> Callable:
    """Decorate an async tool so every call is counted, timed and traced."""

    def decorator(fn):
        # functools.wraps, not a hand-rolled __name__/__doc__ copy: FastMCP builds
        # each tool's JSON schema from the function's signature and __annotations__,
        # so a bare *args/**kwargs wrapper would register every tool with an empty
        # parameter schema. wraps copies __annotations__ and sets __wrapped__, which
        # inspect.signature follows. tests/test_observability.py asserts the
        # registered schema still carries the real parameters.
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            with observe_tool(tool):
                return await fn(*args, **kwargs)

        return wrapper

    return decorator


def shutdown_observability() -> None:
    """Flush and release telemetry on process shutdown. Best-effort — never raises."""
    global _tracer_provider, _meter_provider
    for provider, name in ((_tracer_provider, "trace"), (_meter_provider, "metric")):
        if provider is None:
            continue
        try:
            provider.shutdown()  # flushes the batch processor / metric reader
        except Exception:
            # Same exemption as otel_init_failed: no credential in the OTEL config.
            log.warning("otel_shutdown_failed", provider=name, exc_info=True)
    _tracer_provider = None
    _meter_provider = None
