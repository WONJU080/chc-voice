"""Fish Audio streaming TTS over its live WebSocket protocol.

Fish's live protocol is MessagePack: ``start`` (with a JSON-ish request dict) -> ``text``
-> ``flush`` -> ``stop``; the server answers with ``audio`` events carrying raw PCM and a
``finish`` event.  A connection handles one utterance; a spare connection is opened while
the current one speaks, so consecutive phrases do not pay the handshake twice.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

import ormsgpack
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
from websockets.protocol import State

from ..state import VoiceError

log = logging.getLogger(__name__)

WS_URL = "wss://api.fish.audio/v1/tts/live"
HTTP_URL = "https://api.fish.audio/v1/tts"
DEFAULT_SAMPLE_RATE = 24000
CONNECT_TIMEOUT = 12.0
FINISH_TIMEOUT = 20.0


class FishError(VoiceError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__("Fish TTS", message, status=status)


class FishTTS:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        proxy: str | None = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.proxy = proxy or None
        self.sample_rate = sample_rate
        self._spare_task: asyncio.Task | None = None
        self._spare_voice: str | None = None
        self._closed = False

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "model": self.model}

    async def _connect(self) -> ClientConnection:
        try:
            return await connect(
                WS_URL,
                additional_headers=self._headers,
                proxy=self.proxy,
                open_timeout=CONNECT_TIMEOUT,
                close_timeout=2,
                max_size=None,
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            message = {
                401: "Fish Audio 拒绝了这个 key（HTTP 401）",
                402: "Fish Audio 余额不足（HTTP 402）",
                403: "Fish Audio 拒绝访问（HTTP 403）",
                429: "Fish Audio 限流（HTTP 429）",
            }.get(status, f"Fish Audio 握手失败（HTTP {status}）")
            raise FishError(message, status=status) from exc
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise FishError(f"连不上 Fish Audio：{exc}") from exc

    async def _start_session(self, websocket: ClientConnection, voice_id: str) -> None:
        request: dict[str, Any] = {
            "text": "",
            "sample_rate": self.sample_rate,
            "latency": "balanced",
            "format": "pcm",
            "normalize": True,
            "reference_id": voice_id,
            "prosody": {"speed": 1.0, "volume": 0},
        }
        try:
            await websocket.send(ormsgpack.packb({"event": "start", "request": request}))
        except Exception as exc:
            raise FishError(f"Fish Audio 会话启动失败：{exc}") from exc

    def prewarm(self, voice_id: str) -> None:
        """Open a session in the background so the next phrase skips the handshake."""
        if self._closed or not voice_id:
            return
        if self._spare_task is not None and self._spare_voice == voice_id:
            if not self._spare_task.done():
                return
            if not self._spare_task.cancelled():
                try:
                    ready = self._spare_task.result()
                except Exception:
                    ready = None
                if ready is not None and ready.state is State.OPEN:
                    return
        if self._spare_task is not None and not self._spare_task.done():
            self._spare_task.cancel()
        self._spare_voice = voice_id
        task = asyncio.create_task(self._open_session(voice_id), name="fish-prewarm")
        task.add_done_callback(_consume_task_exception)
        self._spare_task = task

    async def _open_session(self, voice_id: str) -> ClientConnection:
        websocket = await self._connect()
        try:
            await self._start_session(websocket, voice_id)
        except Exception:
            await websocket.close()
            raise
        return websocket

    async def _take_session(self, voice_id: str) -> ClientConnection:
        task, self._spare_task = self._spare_task, None
        if task is not None and self._spare_voice == voice_id:
            try:
                websocket = await task
            except Exception:
                websocket = None
            if websocket is not None and websocket.state is State.OPEN:
                self.prewarm(voice_id)
                return websocket
            if websocket is not None:
                await websocket.close()
        else:
            if task is not None and not task.done():
                task.cancel()
        self.prewarm(voice_id)
        return await self._open_session(voice_id)

    async def close(self) -> None:
        self._closed = True
        task, self._spare_task = self._spare_task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
            return
        if task.cancelled():
            return
        try:
            websocket = task.result()
        except Exception:
            return
        try:
            await websocket.close()
        except Exception:
            pass

    async def synthesize(self, text: str, voice_id: str) -> AsyncIterator[bytes]:
        """Yield raw mono PCM chunks for ``text`` in ``voice_id``."""
        text = (text or "").strip()
        if not text:
            return
        if not voice_id:
            raise FishError("没有配置 Fish 音色")
        websocket = await self._take_session(voice_id)
        try:
            await websocket.send(ormsgpack.packb({"event": "text", "text": text}))
            await websocket.send(ormsgpack.packb({"event": "flush"}))
            await websocket.send(ormsgpack.packb({"event": "stop"}))
            async with asyncio.timeout(FINISH_TIMEOUT):
                async for message in websocket:
                    if not isinstance(message, bytes):
                        continue
                    event = ormsgpack.unpackb(message)
                    if not isinstance(event, dict):
                        continue
                    kind = event.get("event")
                    if kind == "audio":
                        audio = event.get("audio")
                        if isinstance(audio, (bytes, bytearray)) and audio:
                            yield bytes(audio)
                    elif kind == "finish":
                        reason = event.get("reason")
                        if reason == "error":
                            raise FishError("Fish Audio 合成出错")
                        break
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd is not None else None
            raise FishError(f"Fish Audio 在合成完成前关闭了连接（{code}）") from exc
        except TimeoutError as exc:
            raise FishError("Fish Audio 合成超时") from exc
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    async def test(self, voice_id: str, text: str = "こんにちは。") -> int:
        """Synthesize a short line and return how many bytes arrived."""
        total = 0
        async for chunk in self.synthesize(text, voice_id):
            total += len(chunk)
        if total == 0:
            raise FishError("Fish Audio 没有返回音频")
        return total


def _consume_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except Exception:
        pass


async def test_connection(api_key: str, model: str, voice_id: str, *, proxy: str | None = None) -> int:
    tts = FishTTS(api_key, model, proxy=proxy)
    return await tts.test(voice_id)
