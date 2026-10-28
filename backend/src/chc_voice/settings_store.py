"""macOS Keychain storage for the three API keys.

Only the backend touches keys.  The UI receives ``configured`` and the last four
characters.  Nothing here ever logs a key.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ACCOUNT = "chc-voice"

# public service name -> (keychain service, environment fallback, display name)
SERVICES: dict[str, tuple[str, str, str]] = {
    "asr": ("chc-voice.dashscope", "DASHSCOPE_API_KEY", "百炼 ASR"),
    "tts": ("chc-voice.fish", "FISH_AUDIO_API_KEY", "Fish TTS"),
    "llm": ("chc-voice.llm", "CHAT_LLM_API_KEY", "翻译 LLM"),
}


class KeychainError(RuntimeError):
    pass


@dataclass(frozen=True)
class KeyStatus:
    service: str
    configured: bool
    last4: str = ""
    source: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "service": self.service,
            "configured": self.configured,
            "last4": self.last4,
            "source": self.source,
        }


class Keychain:
    """Thin, silent wrapper around ``/usr/bin/security``."""

    def __init__(
        self,
        account: str = ACCOUNT,
        security: str = "/usr/bin/security",
        env: Mapping[str, str] | None = None,
        allow_env: bool = True,
    ) -> None:
        self.account = account
        self.security = security
        self.env = env if env is not None else os.environ
        self.allow_env = allow_env

    # -------------------------------------------------------------- public API
    def get(self, service: str) -> str | None:
        keychain_name, env_name, _ = self._names(service)
        value = self._read(service)
        if value:
            return value
        if self.allow_env:
            env_value = (self.env.get(env_name) or "").strip()
            if env_value:
                return env_value
        return None

    def status(self, service: str) -> KeyStatus:
        keychain_name, env_name, _ = self._names(service)
        value = self._read(service)
        if value:
            return KeyStatus(service, True, value[-4:], "keychain")
        if self.allow_env:
            env_value = (self.env.get(env_name) or "").strip()
            if env_value:
                return KeyStatus(service, True, env_value[-4:], "env")
        return KeyStatus(service, False)

    def set(self, service: str, value: str) -> KeyStatus:
        keychain_name, _, _ = self._names(service)
        value = value.strip()
        if not value:
            raise KeychainError("key 不能为空")
        self._run([
            self.security, "add-generic-password", "-U",
            "-a", self.account, "-s", keychain_name, "-w", value,
        ])
        return self.status(service)

    def delete(self, service: str) -> None:
        keychain_name, _, _ = self._names(service)
        result = subprocess.run(
            [self.security, "delete-generic-password", "-a", self.account, "-s", keychain_name],
            capture_output=True,
        )
        # A missing item is not an error: deleting twice is fine.
        if result.returncode not in (0, 44):
            raise KeychainError(f"无法删除 {service} 的 key（security 退出码 {result.returncode}）")

    def load_env_file(self, path: Path) -> int:
        """Import keys from a development .env into the Keychain.

        The file is read locally, values are passed to ``security`` and never returned.
        Returns how many keys were imported.
        """
        imported = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            for service, (_, env_name, _) in SERVICES.items():
                if name == env_name and value:
                    self.set(service, value)
                    imported += 1
        return imported

    # -------------------------------------------------------------- internals
    def _names(self, service: str) -> tuple[str, str, str]:
        try:
            return SERVICES[service]
        except KeyError as exc:
            raise KeychainError(f"未知服务：{service}") from exc

    def _read(self, service: str) -> str | None:
        keychain_name, _, _ = self._names(service)
        result = subprocess.run(
            [self.security, "find-generic-password", "-a", self.account, "-s", keychain_name, "-w"],
            capture_output=True,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.decode("utf-8", "replace").rstrip("\n")
        return value or None

    def _run(self, args: list[str]) -> None:
        result = subprocess.run(args, capture_output=True)
        if result.returncode != 0:
            # Do not include args: they contain the key.
            raise KeychainError(f"Keychain 写入失败（security 退出码 {result.returncode}）")


def public_key_status(keychain: Keychain) -> dict[str, dict[str, object]]:
    return {service: keychain.status(service).as_dict() for service in SERVICES}
