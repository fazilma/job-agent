"""Run independent network tasks side by side, with a hard overall deadline.

Uses daemon threads (not ThreadPoolExecutor) so a task stuck on a dead network
connection can be abandoned: the run carries on without it, and the process can
still exit, instead of hanging for half an hour.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable


class Abandoned(TimeoutError):
    """The task didn't finish before the deadline; its result was discarded."""


def run_all(tasks: dict[str, Callable[[], Any]], max_workers: int = 4,
            deadline_s: "float | dict[str, float] | None" = None) -> dict[str, tuple[Any, float]]:
    """Returns {name: (result_or_exception, seconds)}. Never raises for a single task.

    deadline_s is one deadline for every task, or {name: seconds} per task (a task missing
    from the dict has none). Times use the monotonic clock, which doesn't advance while the
    machine sleeps, so a sleeping Mac doesn't make tasks look slow or cut them off."""
    if not tasks:
        return {}
    deadlines = dict(deadline_s) if isinstance(deadline_s, dict) else {n: deadline_s for n in tasks}
    done: "queue.Queue[tuple[str, Any, float]]" = queue.Queue()
    slots = threading.Semaphore(max(1, max_workers))
    start = time.monotonic()

    def worker(name: str, fn: Callable[[], Any]) -> None:
        with slots:
            t0 = time.monotonic()
            try:
                res: Any = fn()
            except BaseException as e:            # noqa: BLE001 - report, never crash the run
                res = e
            done.put((name, res, time.monotonic() - t0))

    for name, fn in tasks.items():
        threading.Thread(target=worker, args=(name, fn), daemon=True, name=f"task-{name}").start()

    results: dict[str, tuple[Any, float]] = {}
    pending = set(tasks)
    while pending:
        elapsed = time.monotonic() - start
        for name in sorted(pending):
            d = deadlines.get(name)
            if d is not None and elapsed >= d:
                results[name] = (Abandoned(f"still running after {d:.0f}s; skipped"), elapsed)
                pending.discard(name)
        if not pending:
            break
        waits = [deadlines[n] - elapsed for n in pending if deadlines.get(n) is not None]
        try:
            name, res, secs = done.get(timeout=max(0.0, min(waits)) if waits else None)
        except queue.Empty:
            continue
        if name in pending:
            results[name] = (res, secs)
            pending.discard(name)
    return results
