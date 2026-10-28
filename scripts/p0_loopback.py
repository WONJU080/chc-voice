#!/usr/bin/env python3
"""P0-A: write a tone into BlackHole and record it back from chc-voice."""

from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

import numpy as np
import sounddevice as sd

from chc_voice.audio.devices import ensure_virtual_mic
from chc_voice.audio.output import PcmOutput
from chc_voice.config import ensure_runtime_files

SAMPLE_RATE = 48000
SECONDS = 2.5
THRESHOLD_RMS = 100.0


def main() -> int:
    settings = ensure_runtime_files()
    status = ensure_virtual_mic(settings.audio, create=True)
    print("虚拟麦克风:", status["consumer_name"], "ready=", status["ready"])
    if not status["ready"]:
        print(status.get("guidance") or "虚拟麦克风不可用", file=sys.stderr)
        return 1

    input_name = settings.audio.virtual_device
    frames: list[np.ndarray] = []

    def callback(indata, frames_count, time_info, stream_status):
        if stream_status:
            print("input status:", stream_status, file=sys.stderr)
        frames.append(indata.copy())

    try:
        record = sd.InputStream(
            device=input_name,
            samplerate=SAMPLE_RATE,
            channels=2,
            dtype="int16",
            blocksize=480,
            callback=callback,
        )
        record.start()
    except Exception as exc:
        print(f"无法从 {input_name} 录音：{exc}", file=sys.stderr)
        print("请在 系统设置 → 隐私与安全性 → 麦克风 中允许终端/DeepSeek Harness 使用麦克风。", file=sys.stderr)
        return 2

    output = PcmOutput(settings.audio.blackhole_device, gain=1.0, name="P0 测试音")
    output.start()
    output.write_tone(440.0, 1.2, 0.25)
    time.sleep(SECONDS)
    output.stop()
    record.stop()
    record.close()

    audio = np.concatenate(frames) if frames else np.zeros((1, 2), dtype=np.int16)
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))) if len(audio) else 0.0
    peak = int(np.abs(audio).max()) if len(audio) else 0
    path = Path("artifacts/p0-loopback.wav")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(audio.tobytes())

    print(f"录音：{len(audio)} 帧，RMS={rms:.1f}，峰值={peak}，保存到 {path}")
    if rms < THRESHOLD_RMS:
        print("没有录到测试音。检查 BlackHole 是否安装、Core Audio 是否已重启、麦克风权限是否允许。", file=sys.stderr)
        return 3
    print("P0-A 通过：chc-voice 能读到写入 BlackHole 的音频。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
