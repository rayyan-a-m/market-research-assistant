"""SSE event formatting + a queue-based emitter with heartbeats.

The pipeline runs as a background task that pushes events onto a queue; the
SSE endpoint drains the queue and formats each event as an SSE frame. A
separate heartbeat task pushes a `ping` on an interval so the connection
survives proxy idle timeouts even while a stage is quiet (e.g. waiting for
a user to paste an unreachable source). Decoupling emission from the SSE
generator this way keeps the stage code linear — a stage just calls
`emit(...)`, unaware of framing or heartbeats.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]

_DONE = object()


def format_sse(event_name: str, data: dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(data)}\n\n"


async def run_with_sse(
    task_factory: Callable[[Emit], Awaitable[None]],
    *,
    heartbeat_seconds: int = 15,
) -> AsyncIterator[str]:
    """Run `task_factory(emit)` in the background, yielding SSE frames for
    everything it emits plus periodic heartbeats, until it completes."""
    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def emit(event_name: str, data: dict[str, Any]) -> None:
        await queue.put((event_name, data))

    async def runner() -> None:
        try:
            await task_factory(emit)
        finally:
            await queue.put(_DONE)

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(heartbeat_seconds)
            await queue.put(("ping", {}))

    task = asyncio.create_task(runner())
    hb = asyncio.create_task(heartbeat())
    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                break
            event_name, data = item
            if event_name == "ping":
                yield ": ping\n\n"
            else:
                yield format_sse(event_name, data)
    finally:
        hb.cancel()
        if not task.done():
            task.cancel()
