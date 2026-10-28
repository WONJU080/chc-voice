"""A cancellable queue of committed speech segments."""

from __future__ import annotations

import asyncio
from typing import Generic, TypeVar

T = TypeVar("T")


class SegmentQueue(Generic[T]):
    def __init__(self, maxsize: int = 32) -> None:
        self._queue: asyncio.Queue[T | None] = asyncio.Queue(maxsize=maxsize)

    def put(self, item: T) -> bool:
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            return False

    async def get(self) -> T | None:
        return await self._queue.get()

    def clear(self) -> int:
        removed = 0
        while True:
            try:
                self._queue.get_nowait()
                removed += 1
            except asyncio.QueueEmpty:
                return removed

    def close(self) -> None:
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    @property
    def size(self) -> int:
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()
