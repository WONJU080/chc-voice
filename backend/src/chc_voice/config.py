"""Configuration for chc-voice.

Runtime settings live in ``~/Library/Application Support/chc-voice`` as two small TOML
files (``relay.toml`` and ``voices.toml``).  Repository ``config/`` holds the templates.
API keys are deliberately *not* part of this module: they only live in the macOS Keychain
(see :mod:`chc_voice.settings_store`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

APP_NAME = "chc-voice"
APP_SUPPORT = Path.home() / "Library" / "Application Support" / APP_NAME
DEFAULT_MODE = "zh_direct"
VALID_MODES = ("zh_direct", "zh_to_ja")
DEFAULT_TONE = "casual"
VALID_TONES = ("casual", "neutral", "polite", "keigo")


def project_root() -> Path:
    """The checkout root, so the repository ``config/`` templates can be found."""
    env = os.environ.get("CHC_VOICE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    cwd = Path.cwd()
    if (cwd / "config" / "relay.toml").exists() or (cwd / "backend" / "pyproject.toml").exists():
        return cwd
    return Path(__file__).resolve().parents[3]


def data_dir() -> Path:
    """Where runtime configuration, metrics and logs are kept."""
    env = os.environ.get("CHC_VOICE_DATA_DIR")
    return Path(env).expanduser() if env else APP_SUPPORT


def config_dir() -> Path:
    return data_dir()


def metrics_path() -> Path:
    return data_dir() / "metrics.jsonl"


def log_path() -> Path:
    return data_dir() / "logs" / "chc-voice.log"


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


@dataclass
class AudioSettings:
    input_device: str = ""
    virtual_device: str = "chc-voice"
    blackhole_device: str = "BlackHole 2ch"
    output_gain: float = 0.8
    monitor_gain: float = 0.0
    mic_gain: float = 1.0
    monitor_enabled: bool = False

    def normalised(self) -> "AudioSettings":
        return replace(
            self,
            input_device=self.input_device.strip(),
            virtual_device=self.virtual_device.strip() or "chc-voice",
            blackhole_device=self.blackhole_device.strip() or "BlackHole 2ch",
            output_gain=round(clamp(float(self.output_gain), 0.0, 1.5), 3),
            monitor_gain=round(clamp(float(self.monitor_gain), 0.0, 1.5), 3),
            mic_gain=round(clamp(float(self.mic_gain), 0.0, 3.0), 3),
            monitor_enabled=bool(self.monitor_enabled),
        )


@dataclass
class ApiSettings:
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    bailian_model: str = "qwen-audio-3.1-asr-flash-streaming"


@dataclass
class CommitSettings:
    stable_ms: int = 250
    min_chars: int = 6
    max_chars: int = 40
    pause_flush_ms: int = 450

    def normalised(self, *, default: "CommitSettings") -> "CommitSettings":
        return CommitSettings(
            stable_ms=int(clamp(self.stable_ms, 80, 1500)),
            min_chars=int(clamp(self.min_chars, 1, 30)),
            max_chars=int(clamp(self.max_chars, max(4, self.min_chars), 120)),
            pause_flush_ms=int(clamp(self.pause_flush_ms, 0, 5000)),
        )


@dataclass
class VoiceSettings:
    fish_model: str = "drama-3"
    zh_voice_id: str = ""
    zh_voice_name: str = ""
    ja_voice_id: str = ""
    ja_voice_name: str = ""

    def voice_for(self, mode: str) -> tuple[str, str]:
        if mode == "zh_to_ja":
            return self.ja_voice_id.strip(), self.ja_voice_name.strip()
        return self.zh_voice_id.strip(), self.zh_voice_name.strip()


@dataclass
class UISettings:
    mode: str = DEFAULT_MODE
    show_transcript: bool = True
    show_translation: bool = True

    def normalised(self) -> "UISettings":
        return replace(
            self,
            mode=self.mode if self.mode in VALID_MODES else DEFAULT_MODE,
            show_transcript=bool(self.show_transcript),
            show_translation=bool(self.show_translation),
        )


def detect_system_proxy() -> str:
    """Return macOS's HTTP(S) proxy as a URL, for services that need it (Fish Audio).

    The system proxy on this machine excludes localhost, so the local UI/backend never
    goes through it; only outbound Fish Audio traffic does.
    """
    try:
        output = subprocess.run(
            ["/usr/sbin/scutil", "--proxy"], capture_output=True, text=True, timeout=3
        ).stdout
    except Exception:
        return ""
    if re.search(r"HTTPEnable\s*:\s*1", output):
        host = re.search(r"HTTPProxy\s*:\s*(\S+)", output)
        port = re.search(r"HTTPPort\s*:\s*(\d+)", output)
        if host and port:
            return f"http://{host.group(1)}:{port.group(1)}"
    if re.search(r"HTTPSEnable\s*:\s*1", output):
        host = re.search(r"HTTPSProxy\s*:\s*(\S+)", output)
        port = re.search(r"HTTPSPort\s*:\s*(\d+)", output)
        if host and port:
            return f"http://{host.group(1)}:{port.group(1)}"
    return ""


@dataclass
class NetworkSettings:
    fish_proxy: str = ""

    def normalised(self) -> "NetworkSettings":
        proxy = (self.fish_proxy or os.environ.get("CHC_VOICE_PROXY", "")).strip()
        if not proxy:
            proxy = detect_system_proxy()
        return NetworkSettings(fish_proxy=proxy)


@dataclass
class Settings:
    translation_tone: str = DEFAULT_TONE
    audio: AudioSettings = field(default_factory=AudioSettings)
    api: ApiSettings = field(default_factory=ApiSettings)
    voices: VoiceSettings = field(default_factory=VoiceSettings)
    ui: UISettings = field(default_factory=UISettings)
    network: NetworkSettings = field(default_factory=NetworkSettings)
    commit_zh_direct: CommitSettings = field(
        default_factory=lambda: CommitSettings(stable_ms=250, min_chars=6, max_chars=40, pause_flush_ms=450)
    )
    commit_zh_to_ja: CommitSettings = field(
        default_factory=lambda: CommitSettings(stable_ms=350, min_chars=10, max_chars=60, pause_flush_ms=600)
    )

    # ------------------------------------------------------------------ helpers
    @property
    def mode(self) -> str:
        return self.ui.mode

    def commit_for(self, mode: str) -> CommitSettings:
        return self.commit_zh_to_ja if mode == "zh_to_ja" else self.commit_zh_direct

    def normalised(self) -> "Settings":
        return Settings(
            translation_tone=self.translation_tone if self.translation_tone in VALID_TONES else DEFAULT_TONE,
            audio=self.audio.normalised(),
            api=ApiSettings(
                llm_base_url=(self.api.llm_base_url or "https://api.deepseek.com/v1").strip().rstrip("/"),
                llm_model=(self.api.llm_model or "deepseek-chat").strip(),
                bailian_model=(self.api.bailian_model or "qwen-audio-3.1-asr-flash-streaming").strip(),
            ),
            voices=replace(
                self.voices,
                fish_model=(self.voices.fish_model or "drama-3").strip(),
                zh_voice_id=self.voices.zh_voice_id.strip(),
                zh_voice_name=self.voices.zh_voice_name.strip(),
                ja_voice_id=self.voices.ja_voice_id.strip(),
                ja_voice_name=self.voices.ja_voice_name.strip(),
            ),
            ui=self.ui.normalised(),
            network=self.network.normalised(),
            commit_zh_direct=self.commit_zh_direct.normalised(default=CommitSettings()),
            commit_zh_to_ja=self.commit_zh_to_ja.normalised(default=CommitSettings()),
        )


# --------------------------------------------------------------------- TOML I/O
def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return repr(value)
    raise TypeError(f"unsupported TOML value: {value!r}")


def _dump_table(prefix: tuple[str, ...], data: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    if prefix:
        lines.append("[" + ".".join(prefix) + "]")
    for key, value in scalars.items():
        lines.append(f"{key} = {_toml_value(value)}")
    if scalars and tables:
        lines.append("")
    for key, value in tables.items():
        lines.extend(_dump_table(prefix + (key,), value))
        lines.append("")
    return lines


def _dump_sections(data: dict[str, dict[str, Any]]) -> str:
    lines: list[str] = []
    for name, table in data.items():
        lines.extend(_dump_table((name,), table))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _merge(default: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(default)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(f"无法读取 {path}: {exc}") from exc


class ConfigError(RuntimeError):
    pass


def _env_overrides() -> dict[str, Any]:
    """Environment only overrides defaults; it never contains secrets used by the app."""
    api: dict[str, Any] = {}
    if os.environ.get("CHAT_LLM_BASE_URL", "").strip():
        api["llm_base_url"] = os.environ["CHAT_LLM_BASE_URL"].strip()
    if os.environ.get("CHAT_LLM_MODEL", "").strip():
        api["llm_model"] = os.environ["CHAT_LLM_MODEL"].strip()
    if os.environ.get("BAILIAN_STT_MODEL", "").strip():
        api["bailian_model"] = os.environ["BAILIAN_STT_MODEL"].strip()
    voices: dict[str, Any] = {}
    if os.environ.get("FISH_AUDIO_MODEL", "").strip():
        voices["fish_model"] = os.environ["FISH_AUDIO_MODEL"].strip()
    network: dict[str, Any] = {}
    if os.environ.get("CHC_VOICE_PROXY", "").strip():
        network["fish_proxy"] = os.environ["CHC_VOICE_PROXY"].strip()
    return {"api": api, "voices": voices, "network": network}


def _section(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key, {})
    return current if isinstance(current, dict) else {}


def _commit_from(data: dict[str, Any], default: CommitSettings) -> CommitSettings:
    return CommitSettings(
        stable_ms=int(data.get("stable_ms", default.stable_ms)),
        min_chars=int(data.get("min_chars", default.min_chars)),
        max_chars=int(data.get("max_chars", default.max_chars)),
        pause_flush_ms=int(data.get("pause_flush_ms", default.pause_flush_ms)),
    )


def load_settings(directory: Path | None = None, templates: Path | None = None) -> Settings:
    """Load settings, falling back to repository templates and built-in defaults."""
    directory = directory or config_dir()
    templates = templates or project_root() / "config"
    directory.mkdir(parents=True, exist_ok=True)

    relay_template = _read_toml(templates / "relay.toml")
    voices_template = _read_toml(templates / "voices.toml")
    relay_user = _read_toml(directory / "relay.toml")
    voices_user = _read_toml(directory / "voices.toml")

    defaults = {
        "audio": asdict(AudioSettings()),
        "api": asdict(ApiSettings()),
        "commit": {
            "zh_direct": asdict(CommitSettings()),
            "zh_to_ja": asdict(CommitSettings(stable_ms=350, min_chars=10, max_chars=60, pause_flush_ms=600)),
        },
        "ui": asdict(UISettings()),
        "network": {"fish_proxy": ""},
    }
    voices_defaults = {"fish": asdict(VoiceSettings())}

    relay = _merge(_merge(defaults, relay_template), relay_user)
    relay = _merge(relay, _env_overrides())
    voices = _merge(_merge(voices_defaults, voices_template), voices_user)

    audio_data = _section(relay, "audio")
    api_data = _section(relay, "api")
    translate_data = _section(relay, "translate")
    ui_data = _section(relay, "ui")
    network_data = _section(relay, "network")
    fish_data = _section(voices, "fish")

    settings = Settings(
        translation_tone=str(translate_data.get("tone", DEFAULT_TONE)),
        audio=AudioSettings(
            input_device=str(audio_data.get("input_device", "")),
            virtual_device=str(audio_data.get("virtual_device", "chc-voice")),
            blackhole_device=str(audio_data.get("blackhole_device", "BlackHole 2ch")),
            output_gain=float(audio_data.get("output_gain", 0.8)),
            monitor_gain=float(audio_data.get("monitor_gain", 0.0)),
            mic_gain=float(audio_data.get("mic_gain", 1.0)),
            monitor_enabled=bool(audio_data.get("monitor_enabled", False)),
        ),
        api=ApiSettings(
            llm_base_url=str(api_data.get("llm_base_url", ApiSettings.llm_base_url)),
            llm_model=str(api_data.get("llm_model", ApiSettings.llm_model)),
            bailian_model=str(api_data.get("bailian_model", ApiSettings.bailian_model)),
        ),
        voices=VoiceSettings(
            fish_model=str(fish_data.get("model", "drama-3")),
            zh_voice_id=str(fish_data.get("zh_voice_id", "")),
            zh_voice_name=str(fish_data.get("zh_voice_name", "")),
            ja_voice_id=str(fish_data.get("ja_voice_id", "")),
            ja_voice_name=str(fish_data.get("ja_voice_name", "")),
        ),
        ui=UISettings(
            mode=str(ui_data.get("mode", DEFAULT_MODE)),
            show_transcript=bool(ui_data.get("show_transcript", True)),
            show_translation=bool(ui_data.get("show_translation", True)),
        ),
        network=NetworkSettings(fish_proxy=str(network_data.get("fish_proxy", ""))),
        commit_zh_direct=_commit_from(_section(relay, "commit", "zh_direct"), CommitSettings()),
        commit_zh_to_ja=_commit_from(
            _section(relay, "commit", "zh_to_ja"),
            CommitSettings(stable_ms=350, min_chars=10, max_chars=60, pause_flush_ms=600),
        ),
    )
    return settings.normalised()


def save_settings(settings: Settings, directory: Path | None = None) -> None:
    """Persist settings to the two runtime TOML files."""
    directory = directory or config_dir()
    settings = settings.normalised()
    relay = {
        "translate": {"tone": settings.translation_tone},
        "audio": asdict(settings.audio),
        "api": asdict(settings.api),
        "commit": {
            "zh_direct": asdict(settings.commit_zh_direct),
            "zh_to_ja": asdict(settings.commit_zh_to_ja),
        },
        "ui": asdict(settings.ui),
        "network": asdict(settings.network),
    }
    voices = {
        "fish": {
            "model": settings.voices.fish_model,
            "zh_voice_id": settings.voices.zh_voice_id,
            "zh_voice_name": settings.voices.zh_voice_name,
            "ja_voice_id": settings.voices.ja_voice_id,
            "ja_voice_name": settings.voices.ja_voice_name,
        }
    }
    _atomic_write(directory / "relay.toml", "# chc-voice runtime settings (generated)\n\n" + _dump_sections(relay))
    _atomic_write(directory / "voices.toml", "# chc-voice voice settings (generated)\n\n" + _dump_sections(voices))


def ensure_runtime_files(directory: Path | None = None, templates: Path | None = None) -> Settings:
    """Create runtime TOML files on first launch and return the loaded settings."""
    directory = directory or config_dir()
    templates = templates or project_root() / "config"
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / "relay.toml").exists():
        settings = load_settings(directory, templates)
        save_settings(settings, directory)
    if not (directory / "voices.toml").exists():
        settings = load_settings(directory, templates)
        save_settings(settings, directory)
    return load_settings(directory, templates)
