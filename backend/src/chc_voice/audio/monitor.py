"""Optional headphone monitoring.

Monitor audio never goes into the virtual microphone; it is a second, independent sink.
The default is off because speakers feed the real microphone and create a feedback loop.
"""

from __future__ import annotations

from typing import Any

from ..config import AudioSettings
from .output import PcmOutput

FEEDBACK_WARNING = "建议戴耳机。外放会让真实麦克风再次拾音，形成反馈。"


class Monitor:
    def __init__(self, settings: AudioSettings, *, stream_factory: Any = None) -> None:
        self.settings = settings
        self._output: PcmOutput | None = None
        self._stream_factory = stream_factory
        self.enabled = bool(settings.monitor_enabled)
        self.gain = settings.monitor_gain

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if self.enabled:
            self.start()
        else:
            self.stop()

    def set_gain(self, gain: float) -> None:
        self.gain = max(0.0, min(1.5, float(gain)))
        if self._output is not None:
            self._output.gain = self.gain

    def start(self) -> None:
        if not self.enabled or self._output is not None:
            return
        output = PcmOutput(
            None,
            gain=self.gain,
            name="监听",
            stream_factory=self._stream_factory,
        )
        try:
            output.start()
        except Exception:
            output.stop()
            raise
        self._output = output

    def write_pcm(self, data: bytes, sample_rate: int) -> None:
        if self.enabled and self._output is not None:
            self._output.write_pcm(data, sample_rate)

    def clear(self) -> None:
        if self._output is not None:
            self._output.clear()

    def stop(self) -> None:
        output, self._output = self._output, None
        if output is not None:
            output.stop()

    def health(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "gain": self.gain,
            "output": self._output.health() if self._output is not None else None,
            "warning": FEEDBACK_WARNING if self.enabled else "",
        }
