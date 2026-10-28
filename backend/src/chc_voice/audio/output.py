"""A continuous PCM output stream with silence fill and a small ring buffer.

This is what writes TTS audio into BlackHole.  It intentionally never stops writing while
running: an empty buffer produces digital silence, so apps reading the virtual microphone
do not hear an old fragment after a stop.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable

import numpy as np

from .devices import AudioDeviceError, resolve_output
from .levels import Limiter, dbfs_from_float
from .resample import pcm16_to_float, resample_pcm16

OUTPUT_RATE = 48000
OUTPUT_CHANNELS = 2
BLOCK_FRAMES = 480  # 10 ms at 48 kHz

StreamFactory = Callable[..., Any]


def _default_stream_factory(**kwargs: Any) -> Any:
    import sounddevice as sd

    return sd.OutputStream(**kwargs)


class PcmOutput:
    def __init__(
        self,
        device: str | int | None,
        *,
        samplerate: int = OUTPUT_RATE,
        channels: int = OUTPUT_CHANNELS,
        blocksize: int = BLOCK_FRAMES,
        gain: float = 1.0,
        name: str = "output",
        stream_factory: StreamFactory | None = None,
        max_seconds: float = 8.0,
        limiter: bool = True,
    ) -> None:
        self.device = device
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize
        self.name = name
        self._stream_factory = stream_factory or _default_stream_factory
        self._lock = threading.Lock()
        self._chunks: deque[np.ndarray] = deque()
        self._buffered = 0
        self._max_buffered = int(max_seconds * samplerate)
        self._gain = float(gain)
        self._applied_gain = float(gain)
        self._muted = False
        self._stream: Any = None
        self._running = False
        self._error: str | None = None
        self._level = -120.0
        self._xruns = 0
        self._limiter = Limiter(samplerate) if limiter else None

    # ------------------------------------------------------------------ state
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
    def xruns(self) -> int:
        return self._xruns

    @property
    def gain(self) -> float:
        return self._gain

    @gain.setter
    def gain(self, value: float) -> None:
        self._gain = max(0.0, min(2.0, float(value)))

    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, value: bool) -> None:
        self._muted = bool(value)
        if self._muted:
            self.clear()

    def health(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "running": self._running,
            "device": str(self.device),
            "level_dbfs": round(self._level, 1),
            "pending_ms": round(self.pending_seconds * 1000.0, 1),
            "xruns": self._xruns,
            "error": self._error,
            "muted": self._muted,
        }

    @property
    def pending_seconds(self) -> float:
        with self._lock:
            return self._buffered / float(self.samplerate)

    # ------------------------------------------------------------------ buffer
    def write_pcm(self, data: bytes, sample_rate: int) -> None:
        if not data:
            return
        if sample_rate != self.samplerate:
            data = resample_pcm16(data, sample_rate, self.samplerate, channels=1)
        samples = pcm16_to_float(data)
        self.write_samples(samples)

    def write_samples(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        block = np.ascontiguousarray(samples.astype(np.float32, copy=False))
        with self._lock:
            self._chunks.append(block)
            self._buffered += block.size
            while self._buffered > self._max_buffered and self._chunks:
                dropped = self._chunks.popleft()
                self._buffered -= dropped.size

    def write_tone(self, frequency: float = 440.0, seconds: float = 1.5, amplitude: float = 0.25) -> None:
        length = int(self.samplerate * seconds)
        t = np.arange(length, dtype=np.float32) / self.samplerate
        envelope = np.minimum(1.0, np.minimum(np.arange(length) / (0.01 * self.samplerate), (length - np.arange(length)) / (0.01 * self.samplerate)))
        tone = (amplitude * envelope * np.sin(2.0 * np.pi * frequency * t)).astype(np.float32)
        self.write_samples(tone)

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._buffered = 0
        self._applied_gain = self._gain

    # ------------------------------------------------------------------ stream
    def start(self) -> None:
        if self._running:
            return
        self._error = None
        try:
            device = self._resolve_device()
            stream = self._stream_factory(
                device=device,
                samplerate=self.samplerate,
                channels=self.channels,
                dtype="int16",
                blocksize=self.blocksize,
                callback=self._callback,
                latency="low",
            )
            stream.start()
        except Exception as exc:
            self._error = f"{self.name} 打开失败：{exc}"
            raise AudioDeviceError(self._error) from exc
        self._stream = stream
        self._running = True

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        self._running = False
        self.clear()
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    def _resolve_device(self) -> Any:
        if isinstance(self.device, int):
            return self.device
        if not self.device:
            return None
        device = resolve_output(str(self.device), fallback_default=False)
        return device.index

    # ------------------------------------------------------------------ callback
    def _pull(self, frames: int) -> np.ndarray:
        output = np.zeros(frames, dtype=np.float32)
        filled = 0
        with self._lock:
            while filled < frames and self._chunks:
                chunk = self._chunks[0]
                take = min(frames - filled, chunk.size)
                output[filled:filled + take] = chunk[:take]
                filled += take
                if take == chunk.size:
                    self._chunks.popleft()
                else:
                    self._chunks[0] = chunk[take:]
                self._buffered -= take
        return output

    def _callback(self, outdata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        try:
            if status:
                self._xruns += 1
            if self._muted:
                outdata.fill(0)
                self._level = -120.0
                return
            mono = self._pull(frames)
            # Smooth gain changes over a few blocks to avoid clicks.
            target = self._gain
            self._applied_gain += (target - self._applied_gain) * 0.25
            mono = mono * self._applied_gain
            if self._limiter is not None:
                mono = self._limiter.process(mono)
            mono = np.clip(mono, -1.0, 1.0)
            pcm = (mono * 32767.0).astype(np.int16)
            if self.channels == 1:
                outdata[:, 0] = pcm
            else:
                outdata[:] = pcm[:, None]
            self._level = dbfs_from_float(mono)
        except Exception as exc:  # never kill the audio thread
            self._error = f"{self.name} 写入失败：{exc}"
            outdata.fill(0)
            self._level = -120.0


def play_tone(
    device: str | int,
    *,
    frequency: float = 440.0,
    seconds: float = 1.5,
    amplitude: float = 0.25,
) -> None:
    """Blocking test tone, used by the P0 scripts and the UI test button."""
    output = PcmOutput(device, gain=1.0, name="测试音")
    try:
        output.start()
        output.write_tone(frequency=frequency, seconds=seconds, amplitude=amplitude)
        deadline = time.monotonic() + seconds + 0.4
        while time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        output.stop()
