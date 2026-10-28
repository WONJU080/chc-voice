"""Microphone capture: 20 ms frames at 16 kHz mono, with input gain and metering."""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator

import numpy as np

from ..config import AudioSettings
from ..state import AudioFrame
from .devices import AudioDeviceError, resolve_input
from .levels import SILENCE_DBFS, dbfs_from_int16
from .resample import resample_pcm16

CAPTURE_RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = CAPTURE_RATE * FRAME_MS // 1000  # 320


class MicrophoneCapture:
    def __init__(self, settings: AudioSettings, *, stream_factory: Any = None) -> None:
        self.settings = settings
        self._stream_factory = stream_factory
        self._stream: Any = None
        self._running = False
        self._queue: asyncio.Queue[Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._rate = CAPTURE_RATE
        self._channels = 1
        self._resample = False
        self._error: str | None = None
        self._level = SILENCE_DBFS
        self._xruns = 0
        self._dropped = 0
        self._device_name = ""
        self._last_frame_at = 0.0

    @property
    def running(self) -> bool:
        return self._running

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def level_dbfs(self) -> float:
        return self._level

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def last_frame_at(self) -> float:
        return self._last_frame_at

    async def start(self) -> None:
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=120)
        device = resolve_input(self.settings.input_device)
        self._device_name = device.name
        self._error = None

        # Prefer 16 kHz; fall back to the device's rate, then to stereo input.
        attempts: list[tuple[int, int]] = [(CAPTURE_RATE, 1)]
        if int(device.samplerate) != CAPTURE_RATE:
            attempts.append((int(device.samplerate), 1))
        attempts.append((int(device.samplerate), 2))

        last_error: Exception | None = None
        for rate, channels in attempts:
            try:
                stream = self._open(device.index, rate, channels)
                stream.start()
                self._stream = stream
                self._rate = rate
                self._channels = channels
                self._resample = rate != CAPTURE_RATE
                self._running = True
                return
            except Exception as exc:
                last_error = exc
                try:
                    if "stream" in locals() and stream is not None:
                        stream.close()
                except Exception:
                    pass
        self._error = f"打开麦克风失败：{last_error}"
        raise AudioDeviceError(self._error) from last_error

    def _open(self, device_index: int, rate: int, channels: int) -> Any:
        factory = self._stream_factory
        kwargs = dict(
            device=device_index,
            samplerate=rate,
            channels=channels,
            dtype="int16",
            blocksize=int(rate * FRAME_MS / 1000),
            callback=self._callback,
            latency="low",
        )
        if factory is not None:
            return factory(**kwargs)
        import sounddevice as sd

        return sd.InputStream(**kwargs)

    def _callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        try:
            if status:
                self._xruns += 1
            data = indata.copy()
            if self._channels > 1:
                data = data[:, 0]
            samples = data.astype(np.float32)
            gain = self.settings.mic_gain
            if gain != 1.0:
                samples = np.clip(samples * gain, -32768.0, 32767.0)
            pcm = samples.astype("<i2").tobytes()
            if self._resample:
                pcm = resample_pcm16(pcm, self._rate, CAPTURE_RATE, channels=1)
            level = dbfs_from_int16(pcm)
            self._level = level
            self._last_frame_at = time.monotonic()
            frame = AudioFrame(
                pcm16=pcm,
                samplerate=CAPTURE_RATE,
                timestamp=time.monotonic(),
                level_dbfs=level,
                dropped=self._dropped,
            )
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._enqueue, frame)
        except Exception as exc:  # keep PortAudio's thread alive
            self._error = f"麦克风回调失败：{exc}"

    def _enqueue(self, frame: Any) -> None:
        queue = self._queue
        if queue is None:
            return
        if queue.full():
            try:
                queue.get_nowait()
                self._dropped += 1
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            self._dropped += 1

    async def frames(self) -> AsyncIterator[Any]:
        queue = self._queue
        if queue is None:
            return
        while True:
            frame = await queue.get()
            if frame is None:
                break
            yield frame
            if not self._running and queue.empty():
                break

    async def stop(self) -> None:
        self._running = False
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if self._queue is not None:
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self._level = SILENCE_DBFS

    def health(self) -> dict[str, Any]:
        return {
            "device": self._device_name,
            "running": self._running,
            "rate": self._rate,
            "channels": self._channels,
            "level_dbfs": round(self._level, 1),
            "xruns": self._xruns,
            "dropped": self._dropped,
            "last_frame_age": round(time.monotonic() - self._last_frame_at, 2) if self._last_frame_at else None,
            "error": self._error,
        }
