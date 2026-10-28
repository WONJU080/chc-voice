"""Mode availability, requirements and switching rules."""

from __future__ import annotations

from typing import Iterable

from ..audio.devices import virtual_mic_status
from ..config import Settings
from ..settings_store import Keychain
from ..state import Mode, ModeStatus


class ModeUnavailable(RuntimeError):
    def __init__(self, mode: Mode, missing: Iterable[str]) -> None:
        self.mode = mode
        self.missing = list(missing)
        super().__init__(f"{mode.label} 不可用：{'、'.join(self.missing)}")


class ModeManager:
    def __init__(self, settings: Settings, keychain: Keychain) -> None:
        self.settings = settings
        self.keychain = keychain
        self.mode = Mode.parse(settings.ui.mode)

    def requirements(self, mode: Mode) -> list[str]:
        missing: list[str] = []
        if not self.keychain.status("asr").configured:
            missing.append("百炼 ASR key 未配置")
        if not self.keychain.status("tts").configured:
            missing.append("Fish TTS key 未配置")
        if mode is Mode.ZH_TO_JA:
            if not self.keychain.status("llm").configured:
                missing.append("翻译 LLM key 未配置")
            if not self.settings.voices.ja_voice_id:
                missing.append("日语 Fish 音色未配置")
        else:
            if not self.settings.voices.zh_voice_id:
                missing.append("中文 Fish 音色未配置")
        device = virtual_mic_status(self.settings.audio)
        if not device["ready"]:
            missing.append(device.get("guidance") or "虚拟麦克风未就绪")
        return missing

    def status(self, mode: Mode) -> ModeStatus:
        missing = self.requirements(mode)
        return ModeStatus(mode=mode, available=not missing, missing=missing)

    def statuses(self) -> list[ModeStatus]:
        return [self.status(Mode.ZH_DIRECT), self.status(Mode.ZH_TO_JA)]

    def validate(self, mode: Mode) -> None:
        missing = self.requirements(mode)
        if missing:
            raise ModeUnavailable(mode, missing)

    def select(self, mode: Mode) -> None:
        self.validate(mode)
        self.mode = mode
        self.settings.ui.mode = mode.value
