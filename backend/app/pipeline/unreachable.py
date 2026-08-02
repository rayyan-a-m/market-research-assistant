"""In-process coordination for blocked-source recovery.

When the fetcher can't get usable content from a URL (timeout, navigation
error, or a login/authwall page — see fetcher._classify_markdown) it pauses
on an `asyncio.Event` keyed by the source_id and waits for the user to
decide, via a separate request in the same process:

  • POST /sources/paste  → write pasted text, then wake the fetcher
  • POST /sources/skip   → drop this source, then wake the fetcher

A pause resolves as *skip* or *paste*; the fetcher reads `was_skipped` after
waking to tell the two apart.

This is exactly the state that forces the single-replica constraint
documented in ARCHITECTURE.md / FUTURE_ENHANCEMENTS.md #1 — the waiter map
lives in one process's memory. A restart loses in-flight waiters (the run
surfaces as stalled, not corrupted).
"""

from __future__ import annotations

import asyncio

_waiters: dict[str, asyncio.Event] = {}
_skipped: set[str] = set()


def register_waiter(source_id: str) -> asyncio.Event:
    event = asyncio.Event()
    _waiters[source_id] = event
    return event


def resolve_waiter(source_id: str, *, skip: bool = False) -> bool:
    """Wake a paused fetcher. `skip=True` records that the user chose to drop
    the source rather than supply content. Returns True if a waiter was
    actually waiting (i.e. the run is paused on this source)."""
    event = _waiters.get(source_id)
    if event is None:
        return False
    if skip:
        _skipped.add(source_id)
    event.set()
    return True


def was_skipped(source_id: str) -> bool:
    """True if the pause on `source_id` was resolved by an explicit skip."""
    return source_id in _skipped


def clear_waiter(source_id: str) -> None:
    _waiters.pop(source_id, None)
    _skipped.discard(source_id)
