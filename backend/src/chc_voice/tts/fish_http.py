"""Fish Audio HTTP fallback (the WebSocket path is preferred)."""

from __future__ import annotations

from typing import AsyncIterator

import httpx

from .fish_stream import FishError, HTTP_URL


async def synthesize_http(
    api_key: str,
    model: str,
    text: str,
    voice_id: str,
    *,
    proxy: str | None = None,
    sample_rate: int = 24000,
    timeout: float = 30.0,
) -> AsyncIterator[bytes]:
    headers = {"Authorization": f"Bearer {api_key}", "model": model}
    payload = {
        "text": text,
        "reference_id": voice_id,
        "format": "pcm",
        "sample_rate": sample_rate,
        "normalize": True,
    }
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout, trust_env=False) as client:
            async with client.stream("POST", HTTP_URL, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    detail = body[:200].decode("utf-8", "replace")
                    raise FishError(
                        f"Fish Audio HTTP {response.status_code}：{detail}",
                        status=response.status_code,
                    )
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield chunk
    except httpx.HTTPError as exc:
        raise FishError(f"Fish Audio HTTP 请求失败：{exc}") from exc
