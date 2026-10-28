"""Generation counters used to cancel stale audio work."""

from __future__ import annotations


class Generation:
    def __init__(self) -> None:
        self.value = 0

    def bump(self) -> int:
        self.value += 1
        return self.value

    def current(self, value: int) -> bool:
        return value == self.value
