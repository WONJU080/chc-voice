from __future__ import annotations

from pathlib import Path

from chc_voice.config import AudioSettings, Settings, load_settings, save_settings
from chc_voice.settings_store import KeyStatus, Keychain, public_key_status


def test_defaults_and_toml_roundtrip(tmp_path: Path) -> None:
    templates = Path(__file__).resolve().parents[2] / "config"
    settings = load_settings(tmp_path, templates)
    assert settings.audio.blackhole_device == "BlackHole 2ch"
    assert settings.voices.zh_voice_id  # repository template ships a default
    settings.audio.output_gain = 1.25
    settings.voices.ja_voice_id = "voice-ja"
    settings.ui.mode = "zh_to_ja"
    settings.translation_tone = "keigo"
    settings.commit_zh_to_ja.min_chars = 12
    save_settings(settings, tmp_path)
    loaded = load_settings(tmp_path, templates)
    assert loaded.audio.output_gain == 1.25
    assert loaded.voices.ja_voice_id == "voice-ja"
    assert loaded.ui.mode == "zh_to_ja"
    assert loaded.translation_tone == "keigo"
    assert loaded.commit_zh_to_ja.min_chars == 12


def test_gain_is_clamped(tmp_path: Path) -> None:
    templates = Path(__file__).resolve().parents[2] / "config"
    settings = load_settings(tmp_path, templates)
    settings.audio.output_gain = 99
    settings.audio.mic_gain = -1
    settings.audio.monitor_gain = 5
    normalised = settings.normalised()
    assert normalised.audio.output_gain == 1.5
    assert normalised.audio.mic_gain == 0.0
    assert normalised.audio.monitor_gain == 1.5


def test_env_overrides(tmp_path: Path, monkeypatch) -> None:
    templates = Path(__file__).resolve().parents[2] / "config"
    monkeypatch.setenv("CHAT_LLM_MODEL", "some-model")
    monkeypatch.setenv("CHC_VOICE_PROXY", "http://127.0.0.1:9999")
    settings = load_settings(tmp_path, templates)
    assert settings.api.llm_model == "some-model"
    assert settings.network.fish_proxy == "http://127.0.0.1:9999"


class FakeKeychain:
    def status(self, service: str) -> KeyStatus:
        configured = service in {"asr", "tts"}
        return KeyStatus(service=service, configured=configured, last4="abcd" if configured else "", source="keychain")


def test_public_key_status_has_no_secret() -> None:
    statuses = public_key_status(FakeKeychain())  # type: ignore[arg-type]
    assert statuses["asr"]["configured"] is True
    assert statuses["asr"]["last4"] == "abcd"
    assert statuses["llm"]["configured"] is False
    assert set(statuses) == {"asr", "tts", "llm"}
    assert all("key" not in payload for payload in statuses.values())


def test_keychain_env_fallback(monkeypatch) -> None:
    keychain = Keychain(env={"DASHSCOPE_API_KEY": "sk-test-1234"}, allow_env=True)
    monkeypatch.setattr(keychain, "_read", lambda service: None)
    status = keychain.status("asr")
    assert status.configured and status.source == "env"
    assert status.last4 == "1234"
    assert keychain.get("asr") == "sk-test-1234"
