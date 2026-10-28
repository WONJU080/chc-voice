"""The virtual microphone: BlackHole output plus consumer-facing readiness."""

from __future__ import annotations

import time
from typing import Any

from ..config import AudioSettings
from . import devices
from .output import PcmOutput


class VirtualMic:
    """Writes 48 kHz stereo PCM into BlackHole 2ch.

    Consumers select either the aggregate ``chc-voice`` or, as a fallback, BlackHole 2ch.
    """

    def __init__(self, settings: AudioSettings, *, stream_factory: Any = None) -> None:
        self.settings = settings
        self._output = PcmOutput(
            settings.blackhole_device,
            gain=settings.output_gain,
            name="虚拟麦克风",
            stream_factory=stream_factory,
        )

    @property
    def running(self) -> bool:
        return self._output.running

    @property
    def error(self) -> str | None:
        return self._output.error

    @property
    def level_dbfs(self) -> float:
        return self._output.level_dbfs

    @property
    def pending_seconds(self) -> float:
        return self._output.pending_seconds

    def status(self) -> dict[str, Any]:
        return devices.virtual_mic_status(self.settings)

    def ensure_ready(self) -> dict[str, Any]:
        status = devices.ensure_virtual_mic(self.settings, create=True)
        if not status["ready"]:
            raise devices.AudioDeviceError(status.get("guidance") or "虚拟麦克风不可用")
        return status

    def start(self) -> None:
        self.ensure_ready()
        self._output.start()

    def stop(self) -> None:
        # Clear first so the next callback writes silence immediately, then close after a
        # short drain window.  Consumers hear silence well within 100 ms.
        self._output.clear()
        time.sleep(0.12)
        self._output.stop()

    def clear(self) -> None:
        self._output.clear()

    def mute(self) -> None:
        self._output.muted = True

    def unmute(self) -> None:
        self._output.muted = False

    def set_gain(self, gain: float) -> None:
        self._output.gain = gain

    def write_pcm(self, data: bytes, sample_rate: int) -> None:
        self._output.write_pcm(data, sample_rate)

    def write_tone(self, frequency: float = 440.0, seconds: float = 1.5) -> None:
        self._output.write_tone(frequency=frequency, seconds=seconds)

    def health(self) -> dict[str, Any]:
        return self._output.health()
