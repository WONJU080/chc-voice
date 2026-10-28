"""Fish Audio voice discovery and caching."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class FishVoice:
    id: str
    title: str
    languages: tuple[str, ...]
    tags: tuple[str, ...]
    state: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "languages": list(self.languages),
            "tags": list(self.tags),
            "state": self.state,
        }


def _parse(item: dict[str, Any]) -> FishVoice:
    return FishVoice(
        id=str(item.get("_id") or item.get("id") or ""),
        title=str(item.get("title") or ""),
        languages=tuple(str(lang) for lang in (item.get("languages") or [])),
        tags=tuple(str(tag) for tag in (item.get("tags") or [])),
        state=str(item.get("state") or ""),
    )


async def fetch_voices(
    api_key: str,
    *,
    proxy: str | None = None,
    pages: int = 10,
    page_size: int = 100,
) -> list[FishVoice]:
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(proxy=proxy, timeout=30.0, trust_env=False) as client:
        async def one(page: int) -> list[dict[str, Any]]:
            response = await client.get(
                "https://api.fish.audio/model",
                headers=headers,
                params={"page_size": page_size, "page_number": page},
            )
            if response.status_code != 200:
                raise RuntimeError(f"Fish Audio 音色列表 HTTP {response.status_code}")
            return response.json().get("items") or []

        batches = await asyncio.gather(*(one(page) for page in range(1, pages + 1)))
    voices: list[FishVoice] = []
    for items in batches:
        voices.extend(_parse(item) for item in items)
    return [voice for voice in voices if voice.id and voice.title]


def filter_voices(voices: list[FishVoice], language: str) -> list[FishVoice]:
    prefix = language.lower()
    return [
        voice for voice in voices
        if any(lang.lower().startswith(prefix) for lang in voice.languages)
    ]


class VoiceCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fetched_at = 0.0

    def load(self) -> list[FishVoice]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        self.fetched_at = float(data.get("fetched_at") or 0.0)
        return [
            FishVoice(
                id=str(item.get("id")),
                title=str(item.get("title")),
                languages=tuple(item.get("languages") or []),
                tags=tuple(item.get("tags") or []),
                state=str(item.get("state") or ""),
            )
            for item in data.get("voices") or []
        ]

    def save(self, voices: list[FishVoice]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": time.time(),
            "voices": [voice.as_dict() for voice in voices],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.fetched_at = payload["fetched_at"]

    def stale(self, max_age: float = 24 * 3600) -> bool:
        return not self.path.exists() or time.time() - self.fetched_at > max_age
