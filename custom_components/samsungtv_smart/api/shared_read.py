"""Share status reads that are waiting to start."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any
from weakref import WeakKeyDictionary


class _Read:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.started = False
        self.waiters = 0
        self.shared = False


class QueuedReadPool:
    """Combine reads only before network I/O starts."""

    def __init__(self) -> None:
        self._reads: dict[tuple, _Read] = {}

    async def request(self, key: tuple, read: Callable[[], Awaitable[Any]]) -> Any:
        pending = self._reads.get(key)
        if pending is None or pending.started or pending.task.done():
            pending = _Read()

            async def run() -> Any:
                pending.started = True
                return await read()

            pending.task = asyncio.create_task(run())
            self._reads[key] = pending

            def finished(task: asyncio.Task) -> None:
                if self._reads.get(key) is pending:
                    self._reads.pop(key)
                if not task.cancelled():
                    task.exception()

            pending.task.add_done_callback(finished)
        else:
            pending.shared = True

        pending.waiters += 1
        try:
            result = await asyncio.shield(pending.task)
            return deepcopy(result) if pending.shared else result
        finally:
            pending.waiters -= 1
            if not pending.waiters and not pending.task.done():
                if self._reads.get(key) is pending:
                    self._reads.pop(key)
                # Unlike executor-backed IP Control, aiohttp requests can be cancelled.
                pending.task.cancel()


_SESSION_POOLS: WeakKeyDictionary = WeakKeyDictionary()


async def shared_session_read(session: Any, key: tuple, read: Callable) -> Any:
    try:
        pool = _SESSION_POOLS.get(session)
        if pool is None:
            pool = QueuedReadPool()
            _SESSION_POOLS[session] = pool
    except TypeError:
        # Don't retain sessions that cannot be weak-referenced.
        return await read()
    return await pool.request(key, read)


class _JSONResponse:
    def __init__(self, status: int, data: Any) -> None:
        self.status = status
        self._data = data

    async def json(self) -> Any:
        return self._data


@asynccontextmanager
async def shared_json_get(
    session: Any, url: str, *, headers: dict[str, str]
) -> AsyncIterator[_JSONResponse]:
    async def read() -> tuple[int, Any]:
        async with session.get(url, headers=headers) as response:
            data = await response.json() if response.status == 200 else None
            return response.status, data

    status, data = await shared_session_read(
        session, ("GET", url, tuple(sorted(headers.items()))), read
    )
    yield _JSONResponse(status, data)
