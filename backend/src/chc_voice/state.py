"""Small shared state types and the pipeline state machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Mode(str, Enum):
    ZH_DIRECT = "zh_direct"
    ZH_TO_JA = "zh_to_ja"

    @property
    def label(self) -> str:
        return {"zh_direct": "中文直出", "zh_to_ja": "中文 → 日语"}[self.value]

    @classmethod
    def parse(cls, value: str) -> "Mode":
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(f"未知模式：{value}") from exc


class State(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    LISTENING = "listening"
    RECOGNIZING = "recognizing"
    TRANSLATING = "translating"
    SYNTHESIZING = "synthesizing"
    SPEAKING = "speaking"
    STOPPING = "stopping"
    ERROR = "error"

    @property
    def label(self) -> str:
        return {
            "idle": "就绪",
            "starting": "启动中",
            "listening": "聆听中",
            "recognizing": "识别中",
            "translating": "翻译中",
            "synthesizing": "合成中",
            "speaking": "播放中",
            "stopping": "停止中",
            "error": "错误",
        }[self.value]


@dataclass(frozen=True)
class AudioFrame:
    pcm16: bytes
    samplerate: int
    timestamp: float
    level_dbfs: float
    dropped: int = 0


@dataclass
class Level:
    input_dbfs: float = -120.0
    output_dbfs: float = -120.0
    monitor_dbfs: float = -120.0


@dataclass
class Transcript:
    text: str = ""
    final: bool = False
    at: float = 0.0


@dataclass
class Commit:
    text: str
    reason: str
    at: float
    index: int = 0

    def as_event(self, mode: Mode) -> dict[str, Any]:
        return {"type": "commit", "text": self.text, "reason": self.reason, "mode": mode.value}


@dataclass
class Segment:
    """One unit of work: final text to speak, already translated if needed."""

    source: str
    spoken: str
    mode: Mode
    index: int
    created_at: float
    translation_ms: float | None = None
    first_audio_ms: float | None = None
    total_ms: float | None = None


@dataclass
class ModeStatus:
    mode: Mode
    available: bool
    missing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "label": self.mode.label, "available": self.available, "missing": self.missing}


class VoiceError(RuntimeError):
    """A user-facing error that carries no secret."""

    def __init__(self, service: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.service = service
        self.message = message
        self.status = status

    def as_event(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "error", "service": self.service, "message": self.message}
        if self.status is not None:
            payload["status"] = self.status
        return payload
