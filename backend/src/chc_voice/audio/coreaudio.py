"""Minimal Core Audio device enumeration and aggregate-device management.

macOS provides no command-line tool for creating an Aggregate Device, and the machine's
Command Line Tools Swift toolchain is not usable, so this module talks to the Core Audio
HAL through ``ctypes``.  It is deliberately small: list devices, read their UID, create
and destroy the ``chc-voice`` aggregate whose only sub-device is BlackHole 2ch.
"""

from __future__ import annotations

import ctypes
import plistlib
import time
from ctypes import POINTER, Structure, byref, c_char_p, c_long, c_uint32, c_void_p
from dataclasses import dataclass
from typing import Any

CORE_AUDIO = "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"

_kAudioObjectSystemObject = 1
_kAudioObjectPropertyElementMain = 0
_kCFStringEncodingUTF8 = 0x08000100
_kCFPropertyListImmutable = 0

# FourCC selectors and scopes.
def _fourcc(text: str) -> int:
    return int.from_bytes(text.encode("ascii"), "big")


_kAudioObjectPropertyScopeGlobal = _fourcc("glob")
_kAudioObjectPropertyScopeInput = _fourcc("inpt")
_kAudioObjectPropertyScopeOutput = _fourcc("outp")
_kAudioHardwarePropertyDevices = _fourcc("dev#")
_kAudioDevicePropertyDeviceNameCFString = _fourcc("lnam")
_kAudioDevicePropertyDeviceUID = _fourcc("uid ")
_kAudioDevicePropertyStreamConfiguration = _fourcc("slay")

# Aggregate-device dictionary keys (CoreAudio/AudioHardware.h).
kAudioAggregateDeviceNameKey = "name"
kAudioAggregateDeviceUIDKey = "uid"
kAudioAggregateDeviceMainSubDeviceKey = "master"
kAudioAggregateDeviceIsPrivateKey = "private"
kAudioAggregateDeviceIsStackedKey = "stacked"
kAudioAggregateDeviceSubDeviceListKey = "subdevices"
kAudioSubDeviceUIDKey = "uid"


class AudioObjectPropertyAddress(Structure):
    _fields_ = [
        ("mSelector", c_uint32),
        ("mScope", c_uint32),
        ("mElement", c_uint32),
    ]


class CoreAudioError(RuntimeError):
    def __init__(self, operation: str, status: int):
        self.status = status
        self.operation = operation
        super().__init__(f"{operation}失败：OSStatus {status}{_fourcc_hint(status)}")


def _fourcc_hint(status: int) -> str:
    text = status.to_bytes(4, "big", signed=False).decode("ascii", "replace")
    if all(32 <= ord(c) < 127 for c in text):
        return f"（{text}）"
    return ""


@dataclass(frozen=True)
class CoreAudioDevice:
    id: int
    name: str
    uid: str
    inputs: int
    outputs: int


_ca = ctypes.CDLL(CORE_AUDIO)
_cf = ctypes.CDLL(CORE_FOUNDATION)

_ca.AudioObjectGetPropertyDataSize.argtypes = [
    c_uint32, POINTER(AudioObjectPropertyAddress), c_uint32, c_void_p, POINTER(c_uint32)
]
_ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
_ca.AudioObjectGetPropertyData.argtypes = [
    c_uint32, POINTER(AudioObjectPropertyAddress), c_uint32, c_void_p, POINTER(c_uint32), c_void_p
]
_ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
_ca.AudioHardwareCreateAggregateDevice.argtypes = [c_void_p, POINTER(c_uint32)]
_ca.AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32
_ca.AudioHardwareDestroyAggregateDevice.argtypes = [c_uint32]
_ca.AudioHardwareDestroyAggregateDevice.restype = ctypes.c_int32

_cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
_cf.CFStringGetCString.restype = ctypes.c_bool
_cf.CFDataCreate.argtypes = [c_void_p, c_char_p, c_long]
_cf.CFDataCreate.restype = c_void_p
_cf.CFPropertyListCreateWithData.argtypes = [c_void_p, c_void_p, ctypes.c_ulong, c_void_p, c_void_p]
_cf.CFPropertyListCreateWithData.restype = c_void_p


def _address(selector: int, scope: int = _kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress:
    return AudioObjectPropertyAddress(selector, scope, _kAudioObjectPropertyElementMain)


def _get_string(device: int, selector: int) -> str:
    address = _address(selector)
    size = c_uint32(ctypes.sizeof(c_void_p))
    value = c_void_p()
    status = _ca.AudioObjectGetPropertyData(device, byref(address), 0, None, byref(size), byref(value))
    if status != 0 or not value.value:
        return ""
    buffer = ctypes.create_string_buffer(1024)
    if not _cf.CFStringGetCString(value, buffer, 1024, _kCFStringEncodingUTF8):
        return ""
    return buffer.value.decode("utf-8", "replace")


def _channel_count(device: int, scope: int) -> int:
    address = _address(_kAudioDevicePropertyStreamConfiguration, scope)
    size = c_uint32(0)
    status = _ca.AudioObjectGetPropertyDataSize(device, byref(address), 0, None, byref(size))
    if status != 0 or size.value < 8:
        return 0
    buffer = ctypes.create_string_buffer(size.value)
    status = _ca.AudioObjectGetPropertyData(device, byref(address), 0, None, byref(size), buffer)
    if status != 0:
        return 0
    raw = buffer.raw
    count = int.from_bytes(raw[0:4], "little")
    total = 0
    # AudioBuffer starts at offset 8 on 64-bit (the pointer forces alignment);
    # each AudioBuffer is 16 bytes: UInt32 channels, UInt32 bytes, void *data.
    for index in range(count):
        offset = 8 + index * 16
        if offset + 4 > len(raw):
            break
        total += int.from_bytes(raw[offset:offset + 4], "little")
    return total


def list_devices() -> list[CoreAudioDevice]:
    address = _address(_kAudioHardwarePropertyDevices)
    size = c_uint32(0)
    status = _ca.AudioObjectGetPropertyDataSize(
        _kAudioObjectSystemObject, byref(address), 0, None, byref(size)
    )
    if status != 0:
        raise CoreAudioError("枚举音频设备", status)
    count = size.value // 4
    ids = (c_uint32 * count)()
    status = _ca.AudioObjectGetPropertyData(
        _kAudioObjectSystemObject, byref(address), 0, None, byref(size), ids
    )
    if status != 0:
        raise CoreAudioError("读取音频设备", status)
    devices: list[CoreAudioDevice] = []
    for device_id in ids:
        if not device_id:
            continue
        devices.append(
            CoreAudioDevice(
                id=int(device_id),
                name=_get_string(device_id, _kAudioDevicePropertyDeviceNameCFString),
                uid=_get_string(device_id, _kAudioDevicePropertyDeviceUID),
                inputs=_channel_count(device_id, _kAudioObjectPropertyScopeInput),
                outputs=_channel_count(device_id, _kAudioObjectPropertyScopeOutput),
            )
        )
    return devices


def find_device(name: str | None = None, uid: str | None = None) -> CoreAudioDevice | None:
    for device in list_devices():
        if name is not None and device.name == name:
            return device
        if uid is not None and device.uid == uid:
            return device
    return None


def aggregate_uid(blackhole_uid: str) -> str:
    return f"chc-voice.aggregate.{blackhole_uid}"


def destroy_aggregate(name: str = "chc-voice") -> int:
    destroyed = 0
    prefix = f"{name}."
    for device in list_devices():
        if device.name == name or device.uid.startswith(prefix):
            if _ca.AudioHardwareDestroyAggregateDevice(device.id) == 0:
                destroyed += 1
    if destroyed:
        time.sleep(0.35)
    return destroyed


def create_aggregate(
    name: str = "chc-voice",
    sub_device_name: str = "BlackHole 2ch",
    *,
    replace: bool = False,
) -> CoreAudioDevice:
    """Create (or return) the aggregate device named ``name``.

    The aggregate has exactly one sub-device, so its input is BlackHole's loopback.
    Creating an aggregate does not require administrator rights.
    """
    sub = find_device(name=sub_device_name)
    if sub is None:
        raise CoreAudioError(f"找不到子设备 {sub_device_name}", 0)
    wanted_uid = aggregate_uid(sub.uid)
    existing = find_device(name=name)
    if (
        existing is not None
        and existing.uid == wanted_uid
        and existing.inputs >= 2
        and existing.outputs >= 2
        and not replace
    ):
        return existing
    if existing is not None or replace:
        destroy_aggregate(name)

    description: dict[str, Any] = {
        kAudioAggregateDeviceNameKey: name,
        kAudioAggregateDeviceUIDKey: wanted_uid,
        kAudioAggregateDeviceMainSubDeviceKey: sub.uid,
        kAudioAggregateDeviceIsPrivateKey: False,
        kAudioAggregateDeviceIsStackedKey: False,
        kAudioAggregateDeviceSubDeviceListKey: [{kAudioSubDeviceUIDKey: sub.uid}],
    }
    payload = plistlib.dumps(description, fmt=plistlib.FMT_XML)
    data_ref = _cf.CFDataCreate(None, payload, len(payload))
    if not data_ref:
        raise CoreAudioError("构造 Aggregate Device 描述", 0)
    plist_ref = _cf.CFPropertyListCreateWithData(None, data_ref, _kCFPropertyListImmutable, None, None)
    if not plist_ref:
        raise CoreAudioError("解析 Aggregate Device 描述", 0)

    device_id = c_uint32(0)
    status = _ca.AudioHardwareCreateAggregateDevice(plist_ref, byref(device_id))
    if status != 0:
        raise CoreAudioError("创建 Aggregate Device", status)
    time.sleep(0.6)
    created = find_device(uid=wanted_uid)
    if created is None:
        # Some Core Audio versions take a moment longer; one more short wait.
        time.sleep(0.8)
        created = find_device(uid=wanted_uid)
    if created is None:
        raise CoreAudioError("Aggregate Device 创建后未出现在设备列表", 0)
    return created


def status(name: str = "chc-voice", blackhole_name: str = "BlackHole 2ch") -> dict[str, Any]:
    aggregate = find_device(name=name)
    blackhole = find_device(name=blackhole_name)
    return {
        "aggregate": None
        if aggregate is None
        else {
            "name": aggregate.name,
            "uid": aggregate.uid,
            "id": aggregate.id,
            "inputs": aggregate.inputs,
            "outputs": aggregate.outputs,
        },
        "blackhole": None
        if blackhole is None
        else {
            "name": blackhole.name,
            "uid": blackhole.uid,
            "id": blackhole.id,
            "inputs": blackhole.inputs,
            "outputs": blackhole.outputs,
        },
    }
