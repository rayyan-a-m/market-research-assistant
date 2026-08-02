"""Request correlation and structured logging.

A run touches several stages and a dozen provider calls. When one of them
misbehaves in production, the question is always "show me everything that
happened for *that* request" — which is unanswerable if log lines carry no
shared identifier and are formatted for human eyes rather than for a query.

Two pieces:

1. A `ContextVar` holding the current request id, injected into every log
   record by a logging filter. Handlers never pass the id around; a
   `logger.info(...)` five frames deep is correlated automatically. A
   `ContextVar` (not a thread-local) because the app is asyncio: each task
   gets its own copy, so concurrent requests don't overwrite each other's id.
2. A formatter that emits one flat JSON object per line, which is what log
   aggregators (Azure Monitor, CloudWatch, Datadog) can actually index.

**Why ASGI middleware rather than a FastAPI dependency** for assigning the
id: dependencies run per-route, so a 404, a 422 from request validation, or
an unhandled exception would produce log lines with no id — precisely the
requests worth correlating. ASGI middleware runs unconditionally. The
converse also holds and is why auth and rate limiting are *not* middleware:
they need to run per-route and return a typed value.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"

_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def current_request_id() -> str:
    return _request_id.get()


class RequestIdFilter(logging.Filter):
    """Attaches the current request id to every record.

    A filter rather than a custom Logger subclass: filters apply to records
    from *any* logger, including third-party ones (uvicorn, httpx), so their
    lines get correlated too without those libraries knowing this exists.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


#: Attributes present on every LogRecord. Anything outside this set was
#: passed by the caller via `extra=` and is therefore structured payload
#: worth emitting — which is what lets `logger.info("llm_call", extra={...})`
#: in the metering middleware become queryable fields instead of prose.
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "asctime", "message", "request_id", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str so an unexpected non-serializable value degrades to its
        # repr instead of throwing inside the logging call and losing the line.
        return json.dumps(payload, default=str)


def configure_logging(*, level: str = "INFO", json_logs: bool = True) -> None:
    """Install the formatter and filter on the root handler.

    Plain text is kept as an option because JSON is unreadable when you're
    tailing a local dev server; structured output is for the aggregator, not
    for a person.
    """
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        JsonFormatter()
        if json_logs
        else logging.Formatter("%(levelname)s [%(request_id)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns (or forwards) a request id, times the request, echoes both.

    An inbound `X-Request-ID` is trusted and reused so a trace started at a
    proxy or by the frontend stays one trace end to end; if absent, a short
    id is generated. It's truncated to 12 characters because it exists to be
    grepped and read aloud, not to be globally unique — collisions across a
    log-retention window are harmless.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = (incoming or uuid.uuid4().hex)[:12]
        token = _request_id.set(request_id)
        started = time.monotonic()
        try:
            response = await call_next(request)
        finally:
            # Reset even on an exception, so a failed request can't leak its
            # id into whatever the event loop runs next on this context.
            _request_id.reset(token)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        response.headers[REQUEST_ID_HEADER] = request_id
        logging.getLogger("app.request").info(
            "http_request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "latency_ms": elapsed_ms,
                "request_id": request_id,
            },
        )
        return response
