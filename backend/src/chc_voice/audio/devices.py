"""Audio device discovery and the virtual-microphone readiness check."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import AudioSettings


class AudioDeviceError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    inputs: int
    outputs: int
    samplerate: float
    hostapi: str

    @property
    def label(self) -> str:
        return f"{self.name}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "samplerate": self.samplerate,
            "hostapi": self.hostapi,
        }


def _sd():
    import sounddevice as sd

    return sd


def list_audio_devices() -> list[AudioDevice]:
    sd = _sd()
    try:
        raw = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception as exc:  # PortAudio may fail transiently while devices change
        raise AudioDeviceError(f"无法枚举音频设备：{exc}") from exc
    devices: list[AudioDevice] = []
    for index, device in enumerate(raw):
        host = ""
        try:
            host = hostapis[device["hostapi"]]["name"]
        except Exception:
            pass
        devices.append(
            AudioDevice(
                index=index,
                name=str(device["name"]),
                inputs=int(device["max_input_channels"]),
                outputs=int(device["max_output_channels"]),
                samplerate=float(device["default_samplerate"]),
                hostapi=host,
            )
        )
    return devices


def _matches(device: AudioDevice, wanted: str) -> int:
    if device.name == wanted:
        return 3
    if device.name.casefold() == wanted.casefold():
        return 2
    if wanted.casefold() in device.name.casefold():
        return 1
    return 0


def find_device(
    name_or_index: str | int,
    *,
    kind: str = "input",
    devices: list[AudioDevice] | None = None,
) -> AudioDevice | None:
    devices = devices if devices is not None else list_audio_devices()
    needed = "inputs" if kind == "input" else "outputs"
    candidates = [d for d in devices if getattr(d, needed) > 0]
    if isinstance(name_or_index, int):
        for device in devices:
            if device.index == name_or_index:
                return device
        return None
    wanted = name_or_index.strip()
    if not wanted:
        return None
    best: tuple[int, AudioDevice] | None = None
    for device in candidates:
        score = _matches(device, wanted)
        if score == 0:
            continue
        # Prefer an exact name and a device that has enough channels for stereo.
        channels = getattr(device, needed)
        score = score * 10 + (1 if channels >= 2 else 0)
        if best is None or score > best[0]:
            best = (score, device)
    return best[1] if best else None


def _is_virtual_name(name: str) -> bool:
    lowered = name.casefold()
    return "chc-voice" in lowered or "blackhole" in lowered


def default_input() -> AudioDevice | None:
    sd = _sd()
    try:
        index = sd.default.device[0]
    except Exception:
        index = None
    devices = list_audio_devices()
    if isinstance(index, int) and index >= 0:
        for device in devices:
            if device.index == index:
                # If the system default was pointed at the virtual mic, using it as
                # the relay input would create a feedback loop; fall back to a real mic.
                if not _is_virtual_name(device.name):
                    return device
                break
    return next((d for d in devices if d.inputs > 0 and not _is_virtual_name(d.name)), None)


def default_output() -> AudioDevice | None:
    sd = _sd()
    try:
        index = sd.default.device[1]
    except Exception:
        index = None
    devices = list_audio_devices()
    if isinstance(index, int) and index >= 0:
        for device in devices:
            if device.index == index:
                return device
    return next((d for d in devices if d.outputs > 0), None)


def resolve_input(name_or_index: str | int | None) -> AudioDevice:
    if name_or_index in (None, "", -1):
        device = default_input()
    else:
        device = find_device(name_or_index, kind="input")
    if device is None:
        raise AudioDeviceError(f"找不到输入设备：{name_or_index!r}")
    if _is_virtual_name(device.name):
        raise AudioDeviceError(
            f"输入麦克风不能是 {device.name}：它会把程序自己的输出再录回来，形成自激。请选择真实麦克风。"
        )
    return device


def resolve_output(name_or_index: str | int | None, *, fallback_default: bool = False) -> AudioDevice:
    device = None if name_or_index in (None, "", -1) else find_device(name_or_index, kind="output")
    if device is None and fallback_default:
        device = default_output()
    if device is None:
        raise AudioDeviceError(f"找不到输出设备：{name_or_index!r}")
    return device


def _by_name(devices: list[AudioDevice], name: str, *, channels: int, kind: str) -> AudioDevice | None:
    wanted = name.strip()
    if not wanted:
        return None
    exact = [
        d for d in devices
        if d.name.casefold() == wanted.casefold() and getattr(d, "inputs" if kind == "input" else "outputs") >= channels
    ]
    if exact:
        return exact[0]
    partial = [
        d for d in devices
        if wanted.casefold() in d.name.casefold()
        and getattr(d, "inputs" if kind == "input" else "outputs") >= channels
    ]
    return partial[0] if partial else None


def virtual_mic_status(settings: AudioSettings) -> dict[str, Any]:
    """Report whether consumers can select ``chc-voice``, and why not."""
    try:
        devices = list_audio_devices()
    except AudioDeviceError as exc:
        return {
            "ready": False,
            "blackhole": False,
            "aggregate": False,
            "consumer_name": None,
            "error": str(exc),
            "guidance": "无法读取音频设备列表，请检查 Core Audio。",
        }

    blackhole_out = _by_name(devices, settings.blackhole_device, channels=2, kind="output")
    aggregate_in = _by_name(devices, settings.virtual_device, channels=2, kind="input")
    blackhole_in = _by_name(devices, settings.blackhole_device, channels=2, kind="input")

    ready = blackhole_out is not None and aggregate_in is not None
    consumer = settings.virtual_device if aggregate_in is not None else settings.blackhole_device if blackhole_in else None
    if ready:
        guidance = ""
    elif blackhole_out is None:
        guidance = (
            f"未安装 {settings.blackhole_device}。请先运行 scripts/install_blackhole.sh，"
            "或在终端执行 brew install --cask blackhole-2ch，然后重启 Core Audio。"
        )
    elif aggregate_in is None:
        guidance = (
            f"已找到 {settings.blackhole_device}，但还没有 {settings.virtual_device} Aggregate Device。"
            "点击“创建虚拟麦克风”即可创建，不需要管理员权限。"
        )
    else:
        guidance = "虚拟麦克风不可用。"

    return {
        "ready": ready,
        "blackhole": blackhole_out is not None,
        "aggregate": aggregate_in is not None,
        "blackhole_input": blackhole_in is not None,
        "blackhole_name": settings.blackhole_device,
        "aggregate_name": settings.virtual_device,
        "consumer_name": consumer,
        "aggregate_channels": aggregate_in.inputs if aggregate_in else 0,
        "blackhole_channels": blackhole_out.outputs if blackhole_out else 0,
        "guidance": guidance,
        "error": None,
    }


def ensure_virtual_mic(settings: AudioSettings, *, create: bool = True) -> dict[str, Any]:
    """Create the aggregate device when BlackHole is present but ``chc-voice`` is missing."""
    from . import coreaudio

    status = virtual_mic_status(settings)
    if status["ready"] or not create or not status["blackhole"]:
        return status
    if status["aggregate"]:
        return status
    try:
        coreaudio.create_aggregate(
            name=settings.virtual_device,
            sub_device_name=settings.blackhole_device,
        )
    except Exception as exc:
        status["error"] = str(exc)
        status["guidance"] = f"创建 {settings.virtual_device} 失败：{exc}"
        return status
    return virtual_mic_status(settings)
