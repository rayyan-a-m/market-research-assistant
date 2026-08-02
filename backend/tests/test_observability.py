from __future__ import annotations

import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.observability import (
    REQUEST_ID_HEADER,
    JsonFormatter,
    RequestContextMiddleware,
    RequestIdFilter,
    current_request_id,
)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ok")
    def ok() -> dict[str, str]:
        return {"request_id": current_request_id()}

    @app.get("/boom")
    def boom() -> dict[str, str]:
        raise ValueError("kaboom")

    return app


def test_response_carries_a_request_id() -> None:
    client = TestClient(_app())
    response = client.get("/ok")
    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER]
    # The id visible inside the handler is the one echoed to the client.
    assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]


def test_inbound_request_id_is_forwarded_not_replaced() -> None:
    """A trace started upstream must stay one trace."""
    client = TestClient(_app())
    response = client.get("/ok", headers={REQUEST_ID_HEADER: "upstream-123"})
    assert response.headers[REQUEST_ID_HEADER] == "upstream-123"


def test_ids_differ_between_requests() -> None:
    client = TestClient(_app())
    first = client.get("/ok").headers[REQUEST_ID_HEADER]
    second = client.get("/ok").headers[REQUEST_ID_HEADER]
    assert first != second


def test_context_is_reset_after_a_failed_request() -> None:
    """A request that raises must not leak its id into the next one — the
    reason the ContextVar reset lives in a `finally`."""
    client = TestClient(_app(), raise_server_exceptions=False)
    client.get("/boom")
    assert current_request_id() == "-"


def test_404_still_gets_an_id() -> None:
    """The point of ASGI middleware over a dependency: unmatched routes are
    exactly the requests worth correlating."""
    client = TestClient(_app())
    response = client.get("/no-such-route")
    assert response.status_code == 404
    assert response.headers[REQUEST_ID_HEADER]


def test_json_formatter_emits_extra_fields_as_structured_keys() -> None:
    """`logger.info("llm_call", extra={...})` in the metering middleware has
    to come out as queryable fields, not as prose."""
    record = logging.LogRecord(
        name="app.test", level=logging.INFO, pathname="", lineno=0,
        msg="llm_call", args=None, exc_info=None,
    )
    record.model = "gemini-flash-latest"
    record.input_tokens = 120
    RequestIdFilter().filter(record)

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "llm_call"
    assert payload["level"] == "INFO"
    assert payload["model"] == "gemini-flash-latest"
    assert payload["input_tokens"] == 120
    assert payload["request_id"] == "-"


def test_json_formatter_survives_an_unserializable_value() -> None:
    """A bad `extra=` must not throw inside logging and lose the line."""
    record = logging.LogRecord(
        name="app.test", level=logging.WARNING, pathname="", lineno=0,
        msg="odd", args=None, exc_info=None,
    )
    record.thing = object()

    payload = json.loads(JsonFormatter().format(record))
    assert "thing" in payload
