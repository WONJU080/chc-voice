"""Streaming ASR partials -> stable text -> committed speech chunks.

A partial is cumulative: ``今天``, ``今天天气``, ``今天天气真不错``.  A character is
*stable* once every partial seen in the last ``stable_ms`` agrees on it.  Stable text is
still not spoken immediately: it is held until punctuation, ``max_chars``, a pause, or the
ASR's final result, so Fish gets phrase-sized input instead of one-character fragments.
Nothing that has been committed is ever retracted.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque

from ..state import Commit

SENTENCE_PUNCT = "。！？!?…"
CLAUSE_PUNCT = "，,、；;：:"
ALL_PUNCT = SENTENCE_PUNCT + CLAUSE_PUNCT
# A period only counts when it is followed by whitespace/end; 3.14 must stay intact.
ASCII_PERIOD = "."


@dataclass(frozen=True)
class CommitRule:
    stable_ms: int = 250
    min_chars: int = 6
    max_chars: int = 40
    pause_flush_ms: int = 450


def longest_common_prefix(texts: list[str]) -> str:
    if not texts:
        return ""
    shortest = min(texts, key=len)
    if not shortest:
        return ""
    index = 0
    for chars in zip(*texts):
        if len(set(chars)) != 1:
            break
        index += 1
    return shortest[:index]


def _punct_positions(text: str, start: int, end: int) -> list[int]:
    positions: list[int] = []
    for index in range(start, min(end, len(text))):
        char = text[index]
        if char in ALL_PUNCT:
            positions.append(index + 1)
        elif char == ASCII_PERIOD and (index + 1 >= len(text) or text[index + 1].isspace()):
            positions.append(index + 1)
    return positions


def _is_word_char(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char in "'-")


def safe_cut(text: str, start: int, limit: int) -> int:
    """Cut at ``start + limit`` without splitting an ASCII word or number."""
    end = min(len(text), start + max(1, limit))
    if end >= len(text):
        return len(text)
    while end > start and _is_word_char(text[end - 1]) and _is_word_char(text[end]):
        end -= 1
    return end if end > start else min(len(text), start + max(1, limit))


class StablePrefixCommitter:
    def __init__(self, rule: CommitRule, clock: Callable[[], float] = time.monotonic) -> None:
        self.rule = rule
        self._clock = clock
        self._window: Deque[tuple[float, str]] = deque()
        self._committed = 0
        self._sequence = 0
        self._last_stable_len = 0
        self._last_change = clock()
        self._utterance = ""
        self._last_partial = ""
        self.revisions = 0

    def reset(self) -> None:
        self._window.clear()
        self._committed = 0
        self._last_stable_len = 0
        self._last_change = self._clock()
        self._utterance = ""
        self._last_partial = ""

    @property
    def committed(self) -> int:
        return self._committed

    @property
    def pending_chars(self) -> int:
        return max(0, len(self._utterance) - self._committed)

    def feed(self, partial: str, *, final: bool = False, now: float | None = None) -> list[Commit]:
        now = self._clock() if now is None else now
        partial = (partial or "").replace("\n", " ").strip()
        if not partial and not final:
            return []
        if partial != self._last_partial:
            self._last_partial = partial
            self._last_change = now
        self._utterance = partial
        commits: list[Commit] = []

        if final:
            commits.extend(self._emit(partial[self._committed:], "final", now, force=True))
            self.reset()
            return commits

        if not partial:
            return []

        # Keep only partials from the stability window, plus the current one.
        cutoff = now - self.rule.stable_ms / 1000.0
        self._window.append((now, partial))
        while len(self._window) > 1 and self._window[0][0] < cutoff:
            self._window.popleft()

        stable = longest_common_prefix([text for _, text in self._window])
        stable_len = min(len(stable), len(partial))
        if stable_len < self._committed:
            # The ASR revised text we already spoke.  We cannot take it back; keep going
            # from what was committed and count the revision for diagnostics.
            self.revisions += 1
            stable_len = self._committed
        self._last_stable_len = stable_len
        commits.extend(self._maybe_emit(partial, stable_len, now, final=False))
        return commits

    def tick(self, now: float | None = None) -> list[Commit]:
        """Called periodically so a pause can flush text without a new partial."""
        now = self._clock() if now is None else now
        if not self._utterance:
            return []
        cutoff = now - self.rule.stable_ms / 1000.0
        while len(self._window) > 1 and self._window[0][0] < cutoff:
            self._window.popleft()
        stable = longest_common_prefix([text for _, text in self._window]) if self._window else ""
        stable_len = min(len(stable), len(self._utterance))
        self._last_stable_len = stable_len
        return self._maybe_emit(self._utterance, stable_len, now, final=False)

    # ------------------------------------------------------------------ internals
    def _maybe_emit(self, text: str, stable_len: int, now: float, *, final: bool) -> list[Commit]:
        commits: list[Commit] = []
        while stable_len - self._committed >= self.rule.max_chars:
            limit = safe_cut(text, self._committed, self.rule.max_chars)
            # Prefer punctuation inside this window.
            punct = _punct_positions(text, self._committed, limit)
            cut = punct[-1] if punct else limit
            commits.extend(self._emit(text[self._committed:cut], "max_chars", now))
            if self._committed >= stable_len:
                break

        pending = text[self._committed:stable_len]
        if not pending:
            return commits

        punct = _punct_positions(text, self._committed, stable_len)
        if punct:
            cut = punct[-1]
            segment = text[self._committed:cut]
            # A sentence end is worth speaking even if short; a comma is worth it at
            # half the normal minimum, so mode B does not cut mid-clause at a pause.
            threshold = 2 if any(char in SENTENCE_PUNCT for char in segment) else max(3, self.rule.min_chars // 2)
            if cut - self._committed >= threshold:
                commits.extend(self._emit(segment, "punctuation", now, force=True))
                pending = text[self._committed:stable_len]

        if pending and len(pending) >= self.rule.max_chars:
            cut = safe_cut(text, self._committed, self.rule.max_chars)
            commits.extend(self._emit(text[self._committed:cut], "max_chars", now))
            pending = text[self._committed:stable_len]

        if (
            self.rule.pause_flush_ms > 0
            and pending
            and len(pending) >= self.rule.min_chars
            and (now - self._last_change) * 1000.0 >= self.rule.pause_flush_ms
        ):
            commits.extend(self._emit(pending, "pause", now))
        return commits

    def _emit(self, text: str, reason: str, now: float, *, force: bool = False) -> list[Commit]:
        cleaned = text.strip()
        if not cleaned:
            return []
        if not force and len(cleaned) < self.rule.min_chars:
            return []
        self._committed += len(text)
        commit = Commit(text=cleaned, reason=reason, at=now, index=self._sequence)
        self._sequence += 1
        return [commit]
