"""PCM helpers shared by capture, TTS and the virtual microphone."""

from __future__ import annotations

from math import gcd

import numpy as np


def resample_pcm16(data: bytes, source_rate: int, target_rate: int, channels: int = 1) -> bytes:
    """Resample interleaved little-endian int16 PCM.

    Uses a polyphase filter when SciPy is available and linear interpolation otherwise.
    Voice-band audio at 16 kHz/24 kHz/48 kHz is well served by either.
    """
    if not data or source_rate == target_rate:
        return data
    samples = np.frombuffer(data, dtype="<i2")
    if channels > 1:
        if samples.size % channels:
            samples = samples[: samples.size - (samples.size % channels)]
        samples = samples.reshape(-1, channels)
    source = samples.astype(np.float32)

    divisor = gcd(int(source_rate), int(target_rate))
    up = int(target_rate) // divisor
    down = int(source_rate) // divisor
    try:
        from scipy.signal import resample_poly

        output = resample_poly(source, up, down, axis=0)
    except Exception:
        length = int(round(source.shape[0] * target_rate / source_rate))
        if length <= 0:
            return b""
        old_x = np.linspace(0.0, 1.0, source.shape[0], endpoint=False)
        new_x = np.linspace(0.0, 1.0, length, endpoint=False)
        if channels > 1:
            output = np.stack(
                [np.interp(new_x, old_x, source[:, ch]) for ch in range(channels)], axis=1
            )
        else:
            output = np.interp(new_x, old_x, source)

    output = np.clip(output, -32768.0, 32767.0).astype("<i2")
    return output.tobytes()


def pcm16_to_float(data: bytes) -> np.ndarray:
    samples = np.frombuffer(data, dtype="<i2").astype(np.float32)
    return samples / 32768.0


def float_to_pcm16(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()
