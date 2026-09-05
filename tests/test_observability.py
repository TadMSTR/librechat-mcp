"""Observability tests.

`observability.py` is INSIDE the coverage gate, deliberately. nats-mcp omitted it and
found that the log-sink behaviour — which sink, which logger, whether a file is
created — was therefore never measured at all, while being load-bearing for the
containerised deploy. The same applies here with more force: this server ran for six
weeks emitting no application log lines whatsoever, and nothing noticed.
"""

from __future__ import annotations

import json
import logging

import pytest
import structlog

import librechat_mcp.observability as obs
import librechat_mcp.server as srv


@pytest.fixture(autouse=True)
def _reset_logging():
    """Restore global logging/structlog state around each test.

    `configure_logging()` mutates process-global state (handlers on a named logger,
    structlog's own config). Leaving it set would leak into every later test in the
    session, including ones in other files that assert on captured output.
    """
    app_logger = logging.getLogger(obs._APP_LOGGER)
    saved_handlers = list(app_logger.handlers)
    saved_level, saved_propagate = app_logger.level, app_logger.propagate
    yield
    app_logger.handlers = saved_handlers
    app_logger.level, app_logger.propagate = saved_level, saved_propagate
    structlog.reset_defaults()


# ---------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------


def test_logs_go_to_stdout_as_json(monkeypatch, capsys):
    """The end-to-end claim the README made and the code did not honour."""
    monkeypatch.delenv("LOG_FILE", raising=False)
    obs.configure_logging()

    structlog.get_logger("librechat-mcp").info("hello", agent_id="agent_abc")

    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)  # raises if it is not JSON — which is the assertion
    assert record["event"] == "hello"
    assert record["agent_id"] == "agent_abc"
    assert record["level"] == "info"
    assert "timestamp" in record


def test_a_module_logger_reaches_the_configured_handler(monkeypatch, capsys):
    """The logger-naming trap, pinned.

    `server.py` and `client.py` name their loggers `librechat-mcp.server` and
    `librechat-mcp.client`. The obvious spelling — `structlog.get_logger(__name__)` —
    yields `librechat_mcp.server` with an UNDERSCORE, which is not a child of
    `librechat-mcp` in the logging hierarchy. Handlers are attached to the app logger
    and `propagate` is False, so an underscore-named logger would emit nothing at
    all: exactly the silence this build exists to end. A test on the app logger alone
    would not catch that, because the app logger works either way.
    """
    monkeypatch.delenv("LOG_FILE", raising=False)
    obs.configure_logging()

    srv.log.info("from_the_server_module")
    from librechat_mcp.client import log as client_log

    client_log.info("from_the_client_module")

    events = [json.loads(x)["event"] for x in capsys.readouterr().out.strip().splitlines()]
    assert "from_the_server_module" in events
    assert "from_the_client_module" in events


def test_exc_info_renders_a_traceback(monkeypatch, capsys):
    """`_tool_error` passes `exc_info=True` for unanticipated exceptions.

    Without `format_exc_info` in the processor chain that flag is accepted and
    silently dropped, so the traceback the caller most needs never reaches the log —
    and the call still looks like it succeeded in logging.
    """
    monkeypatch.delenv("LOG_FILE", raising=False)
    obs.configure_logging()

    try:
        raise ValueError("boom")
    except ValueError:
        structlog.get_logger("librechat-mcp").error("tool_error", exc_info=True)

    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "ValueError: boom" in record["exception"]
    assert "Traceback" in record["exception"]


def test_third_party_loggers_are_demoted_to_warning(monkeypatch):
    """httpx logs the full request line at INFO. At the app's level that is a wire
    trace of every call, and it would drown the records that matter."""
    monkeypatch.delenv("LOG_FILE", raising=False)
    obs.configure_logging()
    for name in obs._THIRD_PARTY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


def test_handlers_attach_to_the_named_logger_not_root(monkeypatch):
    """Attaching to root would capture and JSON-render every third-party record at
    the app's level — the untyped-noise pattern remediated in dockhand-mcp #574."""
    monkeypatch.delenv("LOG_FILE", raising=False)
    root_before = list(logging.getLogger().handlers)
    obs.configure_logging()

    assert logging.getLogger(obs._APP_LOGGER).handlers
    assert logging.getLogger(obs._APP_LOGGER).propagate is False
    assert logging.getLogger().handlers == root_before


def test_log_level_is_honoured(monkeypatch, capsys):
    """`LOG_LEVEL` was documented in AGENTS.md and read by no code before v0.2.0."""
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.delenv("LOG_FILE", raising=False)
    obs.configure_logging()

    structlog.get_logger("librechat-mcp").info("should_not_appear")
    structlog.get_logger("librechat-mcp").warning("should_appear")

    out = capsys.readouterr().out
    assert "should_not_appear" not in out
    assert "should_appear" in out


def test_an_unrecognised_log_level_falls_back_to_info(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    monkeypatch.delenv("LOG_FILE", raising=False)
    obs.configure_logging()
    assert logging.getLogger(obs._APP_LOGGER).level == logging.INFO


def test_log_file_is_opt_in_and_written(monkeypatch, tmp_path):
    """No default path: a hardcoded one had every run create a directory and an
    unrotated file whether anyone wanted one."""
    target = tmp_path / "nested" / "librechat.log"
    monkeypatch.setenv("LOG_FILE", str(target))
    obs.configure_logging()

    structlog.get_logger("librechat-mcp").info("to_the_file")
    for handler in logging.getLogger(obs._APP_LOGGER).handlers:
        handler.flush()

    assert json.loads(target.read_text().strip().splitlines()[-1])["event"] == "to_the_file"


def test_a_bare_log_filename_does_not_crash_startup(monkeypatch, tmp_path):
    """`os.path.dirname("librechat.log")` is `""`, and `os.makedirs("")` raises."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOG_FILE", "librechat.log")
    obs.configure_logging()
    assert (tmp_path / "librechat.log").exists()


def test_an_unwritable_log_path_disables_the_file_sink_without_crashing(
    monkeypatch, tmp_path, capsys
):
    """A read-only container filesystem must not take the server down — stdout is
    already attached and is the primary sink."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("LOG_FILE", str(blocker / "sub" / "x.log"))

    obs.configure_logging()  # must not raise

    assert "file logging disabled" in capsys.readouterr().err
    handlers = logging.getLogger(obs._APP_LOGGER).handlers
    assert not any(isinstance(h, logging.FileHandler) for h in handlers)

    structlog.get_logger("librechat-mcp").info("still_logging")
    assert "still_logging" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# OTEL — opt-in, and silent when unconfigured
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_otel():
    """OTEL state is module-global and memoised; a test that left it set would make
    every later test's `_init_otel()` a no-op."""
    yield
    obs._tracer = None
    obs._tracer_provider = None
    obs._meter_provider = None
    obs._call_counter = None
    obs._duration_histogram = None
    obs._otel_failed = False


def test_otel_stays_silent_when_the_endpoint_is_unset(monkeypatch, capsys):
    """The intended disabled path. A warning here would fire on every deployment that
    simply does not use OTEL, and warnings that always fire get filtered out."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert obs.get_tracer() is None
    assert obs._otel_failed is False
    assert capsys.readouterr().err == ""


def test_a_failing_otel_init_warns_once_and_does_not_retry(monkeypatch):
    """`_otel_failed` is a distinct flag rather than an overloaded None precisely so
    'never tried' stays distinguishable from 'tried and failed'."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    warnings = []
    monkeypatch.setattr(obs.log, "warning", lambda *a, **k: warnings.append(a))

    import builtins

    real_import = builtins.__import__

    def exploding_import(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError("no [otel] extra installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", exploding_import)

    assert obs.get_tracer() is None
    assert obs._otel_failed is True
    assert len(warnings) == 1

    obs.get_tracer()  # second call must not retry the import or warn again
    assert len(warnings) == 1


def test_observe_tool_is_a_no_op_when_otel_is_off(monkeypatch):
    """The tools are wrapped unconditionally, so this path runs on every call in
    every deployment. It must not raise and must not need a backend."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    with obs.observe_tool("list_agents", agent_id="a1"):
        pass


def test_observe_tool_reraises_and_records_the_outcome(monkeypatch):
    """An exception must propagate — swallowing it here would convert a failure into
    a silent success, which is this repo's entire failure mode."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    with pytest.raises(ValueError):
        with obs.observe_tool("list_agents"):
            raise ValueError("boom")


def test_shutdown_is_safe_when_nothing_was_initialised():
    obs.shutdown_observability()  # must not raise


def test_shutdown_flushes_both_providers_and_swallows_failures():
    """Best-effort by design: a failing flush at exit must not become a crash at
    exit, but it must be visible."""

    class Exploding:
        def shutdown(self):
            raise RuntimeError("collector gone")

    class Recording:
        def __init__(self):
            self.shut_down = False

        def shutdown(self):
            self.shut_down = True

    obs._tracer_provider = Exploding()
    obs._meter_provider = Recording()
    meter = obs._meter_provider

    obs.shutdown_observability()  # must not raise

    assert meter.shut_down is True
    assert obs._tracer_provider is None
    assert obs._meter_provider is None


# ---------------------------------------------------------------------------
# instrument()
# ---------------------------------------------------------------------------


async def test_instrument_preserves_the_wrapped_function_signature():
    """FastMCP builds each tool's JSON schema from the signature and
    `__annotations__`. A bare `*args/**kwargs` wrapper would register every tool with
    an EMPTY parameter schema — the tools would still exist, still be callable in
    tests, and be unusable by any model."""

    @obs.instrument("sample")
    async def sample(agent_id: str, limit: int = 20) -> dict:
        """Docstring retained."""
        return {"agent_id": agent_id, "limit": limit}

    import inspect

    params = inspect.signature(sample).parameters
    assert list(params) == ["agent_id", "limit"]
    assert sample.__name__ == "sample"
    assert sample.__doc__ == "Docstring retained."
    assert await sample("a1") == {"agent_id": "a1", "limit": 20}


async def test_the_registered_tools_still_carry_their_real_parameters():
    """The same claim, against the actual FastMCP registration rather than a sample.

    This is the assertion that would fail if `instrument` were ever reimplemented
    without `functools.wraps` — checking a locally-defined function would not.
    """
    tools = {t.name: t for t in await srv.mcp.list_tools()}

    assert set(tools["list_agents"].parameters["properties"]) == {"search", "limit"}
    assert set(tools["get_agent"].parameters["properties"]) == {"agent_id"}
    # All six tools are wrapped, so an empty schema on any of them is the same bug.
    assert not [
        name
        for name, tool in tools.items()
        if not tool.parameters.get("properties")
        and name not in {"list_tools"}  # genuinely takes no arguments
    ]


# ---------------------------------------------------------------------------
# OTEL — the real SDK path
#
# These do NOT use importorskip. CI installs ".[dev,otel]" precisely so this file
# cannot skip itself into a green result: a [dev]-only install would make the whole
# section vanish while still reporting success, which is the failure mode the extra
# exists to prevent (vikunja#336). If the SDK is missing, these must fail loudly.
#
# The endpoint below is deliberately unreachable and that is not a flaw. The gRPC
# OTLP exporter does not connect at construction, so init succeeds against a dead
# host — which is exactly why "OTEL is configured" is never evidence that anything
# arrives. Whether spans and metrics actually land is a live check, not a unit test.
# ---------------------------------------------------------------------------


def test_otel_initialises_against_a_configured_endpoint(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:14317")

    tracer = obs.get_tracer()

    assert tracer is not None
    assert obs._otel_failed is False
    assert obs._call_counter is not None
    assert obs._duration_histogram is not None
    obs.shutdown_observability()


def test_otel_init_is_memoised(monkeypatch):
    """Re-running init per tool call would rebuild two exporters every time."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:14317")
    first = obs.get_tracer()
    assert obs.get_tracer() is first
    obs.shutdown_observability()


def test_observe_tool_records_a_call_and_a_duration(monkeypatch):
    """Metrics carry the latency signal, not spans — a per-call span export blocks on
    the collector round trip (121s vs 0.1s, measured on dockhand-mcp)."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:14317")
    obs.get_tracer()

    counted: list[tuple] = []
    recorded: list[tuple] = []
    monkeypatch.setattr(
        obs, "_call_counter", type("C", (), {"add": lambda s, n, la: counted.append((n, la))})()
    )
    monkeypatch.setattr(
        obs,
        "_duration_histogram",
        type("H", (), {"record": lambda s, v, la: recorded.append((v, la))})(),
    )

    with obs.observe_tool("list_agents", agent_id="a1"):
        pass

    assert counted == [(1, {"tool": "list_agents", "outcome": "ok"})]
    assert len(recorded) == 1
    assert recorded[0][1] == {"tool": "list_agents", "outcome": "ok"}
    assert recorded[0][0] >= 0
    obs.shutdown_observability()


def test_observe_tool_labels_a_failure_with_the_exception_class(monkeypatch):
    """`outcome` must distinguish failures, or the counter reports every call as ok
    and the metric says the server is healthy while every tool errors."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:14317")
    obs.get_tracer()

    counted: list[tuple] = []
    monkeypatch.setattr(
        obs, "_call_counter", type("C", (), {"add": lambda s, n, la: counted.append((n, la))})()
    )

    with pytest.raises(ValueError):
        with obs.observe_tool("list_agents"):
            raise ValueError("boom")

    assert counted == [(1, {"tool": "list_agents", "outcome": "ValueError"})]
    obs.shutdown_observability()


def test_shutdown_flushes_the_real_providers(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:14317")
    obs.get_tracer()
    assert obs._tracer_provider is not None

    obs.shutdown_observability()

    assert obs._tracer_provider is None
    assert obs._meter_provider is None
