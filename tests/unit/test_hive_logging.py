import io
import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hive_logging import (
    HiveJsonFormatter,
    bind_context,
    configure_logging,
    install_fastapi_logging,
    log_event,
    log_if_slow,
    reset_context,
)


def _formatted_record(message="changed", **extra):
    record = logging.LogRecord("test", logging.INFO, __file__, 1, message, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return json.loads(HiveJsonFormatter().format(record))


def test_json_formatter_emits_stable_event_and_fields(monkeypatch):
    monkeypatch.setenv("HIVE_SERVICE", "test-service")
    token = bind_context(request_id="req-1")
    try:
        data = _formatted_record(
            hive_event="session.created",
            hive_fields={"session_id": "s-1", "model": "sonnet"},
        )
    finally:
        reset_context(token)

    assert data["service"] == "test-service"
    assert data["event"] == "session.created"
    assert data["request_id"] == "req-1"
    assert data["session_id"] == "s-1"


def test_json_formatter_recursively_redacts_and_bounds_sensitive_data():
    data = _formatted_record(
        hive_fields={
            "authorization": "Bearer abc",
            "nested": {"api_token": "abc", "safe": "yes"},
            "password_hint": "nope",
        }
    )
    encoded = json.dumps(data)
    assert "Bearer abc" not in encoded
    assert '"api_token": "abc"' not in encoded
    assert data["nested"]["safe"] == "yes"
    assert data["authorization"] == "[REDACTED]"


def test_log_event_keeps_logging_when_field_is_not_json_serializable(caplog):
    logger = logging.getLogger("hive-test")
    with caplog.at_level(logging.INFO):
        log_event(logger, "object.changed", value=object())
    assert caplog.records[-1].hive_event == "object.changed"
    assert isinstance(caplog.records[-1].hive_fields["value"], str)


def test_configure_logging_suppresses_polling_client_info_logs():
    configure_logging("test-service")

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert logging.getLogger("aiohttp.access").level == logging.WARNING


def test_log_if_slow_warns_only_after_threshold(caplog):
    logger = logging.getLogger("hive-test")
    with caplog.at_level(logging.INFO):
        log_if_slow(logger, "turn.slow", 30_000, session_id="fast")
        log_if_slow(logger, "turn.slow", 30_000.1, session_id="slow")

    slow_records = [r for r in caplog.records if getattr(r, "hive_event", None) == "turn.slow"]
    assert len(slow_records) == 1
    assert slow_records[0].levelno == logging.WARNING
    assert slow_records[0].hive_fields["session_id"] == "slow"


def test_fastapi_logging_correlates_response_and_events(caplog):
    app = FastAPI()
    logger = logging.getLogger("hive-http-test")
    install_fastapi_logging(app, logger, component="test-api")
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(HiveJsonFormatter())
    logger.addHandler(handler)

    @app.post("/work")
    async def work():
        log_event(logger, "work.performed")
        return {"ok": True}

    try:
        with caplog.at_level(logging.INFO):
            response = TestClient(app).post(
                "/work", headers={"x-request-id": "req-42"}
            )
    finally:
        logger.removeHandler(handler)

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req-42"
    formatted = [json.loads(line) for line in output.getvalue().splitlines()]
    relevant = [item for item in formatted if item.get("event") in {
        "http.request.started", "work.performed", "http.request.completed",
    }]
    assert [item["event"] for item in relevant] == [
        "http.request.started", "work.performed", "http.request.completed",
    ]
    assert all(item["request_id"] == "req-42" for item in relevant)
