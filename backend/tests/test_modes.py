from __future__ import annotations

import pytest

from chc_voice.config import AudioSettings, Settings
from chc_voice.relay.modes import ModeManager, ModeUnavailable
from chc_voice.settings_store import KeyStatus
from chc_voice.state import Mode


class FakeKeychain:
    def __init__(self, services=("asr", "tts", "llm")) -> None:
        self.services = set(services)

    def status(self, service: str) -> KeyStatus:
        configured = service in self.services
        return KeyStatus(service=service, configured=configured, last4="abcd" if configured else "")


READMEY = {
    "ready": True,
    "blackhole": True,
    "aggregate": True,
    "consumer_name": "chc-voice",
    "guidance": "",
}


def make_settings() -> Settings:
    settings = Settings()
    settings.audio = AudioSettings()
    settings.voices.zh_voice_id = "zh"
    settings.voices.ja_voice_id = "ja"
    return settings


def test_both_modes_available(monkeypatch) -> None:
    monkeypatch.setattr("chc_voice.relay.modes.virtual_mic_status", lambda audio: READMEY)
    manager = ModeManager(make_settings(), FakeKeychain())
    assert manager.status(Mode.ZH_DIRECT).available
    assert manager.status(Mode.ZH_TO_JA).available


def test_mode_b_requires_llm_key(monkeypatch) -> None:
    monkeypatch.setattr("chc_voice.relay.modes.virtual_mic_status", lambda audio: READMEY)
    manager = ModeManager(make_settings(), FakeKeychain(services=("asr", "tts")))
    status = manager.status(Mode.ZH_TO_JA)
    assert not status.available
    assert any("翻译 LLM" in item for item in status.missing)


def test_mode_a_requires_voice(monkeypatch) -> None:
    monkeypatch.setattr("chc_voice.relay.modes.virtual_mic_status", lambda audio: READMEY)
    settings = make_settings()
    settings.voices.zh_voice_id = ""
    manager = ModeManager(settings, FakeKeychain())
    status = manager.status(Mode.ZH_DIRECT)
    assert not status.available
    assert any("中文 Fish 音色" in item for item in status.missing)


def test_mode_requires_virtual_mic(monkeypatch) -> None:
    monkeypatch.setattr(
        "chc_voice.relay.modes.virtual_mic_status",
        lambda audio: {"ready": False, "blackhole": True, "aggregate": False, "guidance": "请创建 chc-voice"},
    )
    manager = ModeManager(make_settings(), FakeKeychain())
    status = manager.status(Mode.ZH_DIRECT)
    assert not status.available
    assert status.missing[-1] == "请创建 chc-voice"


def test_validate_raises(monkeypatch) -> None:
    monkeypatch.setattr("chc_voice.relay.modes.virtual_mic_status", lambda audio: READMEY)
    manager = ModeManager(make_settings(), FakeKeychain(services=("asr", "tts")))
    with pytest.raises(ModeUnavailable) as info:
        manager.validate(Mode.ZH_TO_JA)
    assert "翻译 LLM" in str(info.value)


def test_select_persists_mode(monkeypatch) -> None:
    monkeypatch.setattr("chc_voice.relay.modes.virtual_mic_status", lambda audio: READMEY)
    settings = make_settings()
    manager = ModeManager(settings, FakeKeychain())
    manager.select(Mode.ZH_TO_JA)
    assert manager.mode is Mode.ZH_TO_JA
    assert settings.ui.mode == "zh_to_ja"
