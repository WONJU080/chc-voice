"""JSONL metrics: latency, queue depth and errors, never transcript content."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass
class SegmentMetrics:
    """Latency of one spoken segment, measured from audio time where possible."""

    mode: str
    commit_at: float
    translation_ms: float | None = None
    tts_first_audio_ms: float | None = None
    total_ms: float | None = None
    chars: int = 0
    source_chars: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "translation_ms": _round(self.translation_ms),
            "tts_first_audio_ms": _round(self.tts_first_audio_ms),
            "total_ms": _round(self.total_ms),
            "chars": self.chars,
            "source_chars": self.source_chars,
        }


def _round(value: float | None) -> int | None:
    return None if value is None else int(round(value))


@dataclass
class MetricsLog:
    path: Path
    enabled: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def write(self, event: str, **fields: Any) -> None:
        if not self.enabled:
            return
        record = {"ts": now_iso(), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            # Metrics must never break audio.
            pass

    def segment(self, metrics: SegmentMetrics) -> None:
        self.write("segment", **metrics.as_dict())

    def error(self, service: str, message: str, status: int | None = None) -> None:
        # Messages are constructed by this project and never contain keys.
        self.write("error", service=service, message=message, status=status)

    def startup(self, mode: str, **extra: Any) -> None:
        self.write("startup", mode=mode, pid=os.getpid(), **extra)

    def shutdown(self) -> None:
        self.write("shutdown")


def monotonic_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000.0
