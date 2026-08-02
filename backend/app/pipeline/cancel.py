"""In-process cooperative cancellation for runs.

Same single-replica model as the unreachable-source waiter (see
ARCHITECTURE.md): the cancel endpoint flags a run's id in memory; the
pipeline checks the flag at each stage boundary and bails, leaving the run
CANCELLED. On a multi-replica deployment this would move to Redis (the
documented scaling path).
"""

from __future__ import annotations

_cancelled: set[str] = set()


def request_cancel(run_id: str) -> None:
    _cancelled.add(run_id)


def is_cancelled(run_id: str) -> bool:
    return run_id in _cancelled


def clear_cancel(run_id: str) -> None:
    _cancelled.discard(run_id)
