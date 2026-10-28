#!/usr/bin/env python3
"""Print Core Audio / PortAudio devices and chc-voice readiness as JSON."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from chc_voice.audio import coreaudio
from chc_voice.audio.devices import list_audio_devices, virtual_mic_status
from chc_voice.config import ensure_runtime_files


def main() -> int:
    settings = ensure_runtime_files()
    core = [
        {"id": device.id, "name": device.name, "uid": device.uid, "inputs": device.inputs, "outputs": device.outputs}
        for device in coreaudio.list_devices()
    ]
    portaudio = [device.as_dict() for device in list_audio_devices()]
    status = virtual_mic_status(settings.audio)
    print(json.dumps({"coreaudio": core, "portaudio": portaudio, "virtual_mic": status}, ensure_ascii=False, indent=2))
    return 0 if status["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
