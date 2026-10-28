"""Level metering and a cheap, click-free limiter."""

from __future__ import annotations

import math

import numpy as np

SILENCE_DBFS = -120.0


def dbfs_from_int16(data: bytes | np.ndarray) -> float:
    if isinstance(data, bytes):
        if not data:
            return SILENCE_DBFS
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32)
    else:
        samples = data.astype(np.float32, copy=False)
    if samples.size == 0:
        return SILENCE_DBFS
    rms = float(np.sqrt(np.mean(samples * samples))) / 32768.0
    return max(SILENCE_DBFS, 20.0 * math.log10(rms)) if rms > 1e-7 else SILENCE_DBFS


def dbfs_from_float(samples: np.ndarray) -> float:
    if samples.size == 0:
        return SILENCE_DBFS
    rms = float(np.sqrt(np.mean(samples * samples)))
    return max(SILENCE_DBFS, 20.0 * math.log10(rms)) if rms > 1e-7 else SILENCE_DBFS


class Limiter:
    """Look-ahead-free peak limiter with a smooth gain ramp.

    Every processing block starts at the previous block's gain and ramps to the gain the
    block's peak requires.  This is not a mastering limiter; it exists to keep the
    virtual microphone signal below clipping while staying cheap enough for a 10 ms
    audio callback.
    """

    def __init__(self, sample_rate: int, threshold: float = 0.95, attack_ms: float = 2.0, release_ms: float = 120.0):
        self.sample_rate = sample_rate
        self.threshold = max(0.1, min(1.0, threshold))
        self.attack_ms = attack_ms
        self.release_ms = release_ms
        self.gain = 1.0

    def process(self, block: np.ndarray) -> np.ndarray:
        if block.size == 0:
            return block
        peak = float(np.max(np.abs(block)))
        target = 1.0 if peak <= self.threshold else self.threshold / peak
        if target < self.gain:
            tau = max(0.0005, self.attack_ms / 1000.0)
        else:
            tau = max(0.01, self.release_ms / 1000.0)
        block_seconds = block.shape[0] / float(self.sample_rate)
        coefficient = 1.0 - math.exp(-block_seconds / tau)
        end = self.gain + (target - self.gain) * coefficient
        # Smoothly ramp from the previous gain to this block's gain.
        ramp = np.linspace(self.gain, end, block.shape[0], dtype=np.float32)
        self.gain = end
        output = block * ramp if block.ndim == 1 else block * ramp[:, None]
        return np.clip(output, -1.0, 1.0)
