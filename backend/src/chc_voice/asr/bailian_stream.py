"""Alibaba Cloud Bailian (DashScope) realtime speech recognition over WebSocket.

The protocol is DashScope's duplex ASR task: ``run-task`` -> ``task-started`` -> binary
16 kHz mono PCM -> ``result-generated`` sentences -> ``finish-task``.  Partials are
cumulative; the final flag arrives with ``sentence_end``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from ..state import VoiceError

log = logging.getLogger(__name__)

REGION_URLS: dict[str, str] = {
    "beijing": "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
    "singapore": "wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference",
}
REGION_NAMES = {"beijing": "北京", "singapore": "新加坡"}

SAMPLE_RATE = 16000
PACKET_BYTES = SAMPLE_RATE * 2 // 10  # 100 ms
START_TIMEOUT = 10.0
FINISH_TIMEOUT = 4.0
KEEPALIVE_TIMEOUT = 1.0

PARAMETERS: dict[str, Any] = {
    "format": "pcm",
    "sample_rate": SAMPLE_RATE,
    "vad_model": "near_meeting_16k",
    "language_hints": ["zh"],
    "heartbeat": True,
}


class ASRError(VoiceError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__("百炼 ASR", message, status=status)


class ASRCheckError(ASRError):
    pass


class BailianASR:
    """One DashScope recognition task.

    Callbacks run on the event loop that drives :meth:`start`:
    ``on_partial(text)`` for each cumulative sentence, ``on_final(text)`` when a sentence
    ends, ``on_error(exception)`` on a fatal condition and ``on_state(state)`` for
    diagnostics.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        region: str | None = None,
        proxy: str | None = None,
        on_partial: Callable[[str], None] | None = None,
        on_final: Callable[[str], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        on_state: Callable[[str], None] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.region = region
        self.proxy = proxy or None
        self.on_partial = on_partial or (lambda _text: None)
        self.on_final = on_final or (lambda _text: None)
        self.on_error = on_error or (lambda _error: None)
        self.on_state = on_state or (lambda _state: None)
        self._websocket: ClientConnection | None = None
        self._task_id: str | None = None
        self._receive_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._held = bytearray()
        self._last_send = 0.0
        self._finished = asyncio.Event()
        self._closing = False
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> str:
        regions = [self.region] if self.region else list(REGION_URLS)
        refused: list[tuple[str, int]] = []
        last_error: Exception | None = None
        for region in regions:
            try:
                websocket = await connect(
                    REGION_URLS[region],
                    additional_headers={"Authorization": f"bearer {self.api_key}"},
                    proxy=self.proxy,
                    open_timeout=8,
                    close_timeout=2,
                    max_size=None,
                )
            except InvalidStatus as exc:
                status = exc.response.status_code
                if status in (401, 403):
                    refused.append((region, status))
                    continue
                last_error = exc
                break
            except (OSError, TimeoutError, WebSocketException) as exc:
                last_error = exc
                continue
            try:
                self._task_id = await self._run_task(websocket)
            except Exception:
                await websocket.close()
                raise
            self._websocket = websocket
            self.region = region
            self._connected = True
            self._finished = asyncio.Event()
            self._receive_task = asyncio.create_task(self._receive_loop(), name="asr-receive")
            self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="asr-keepalive")
            self.on_state("asr_connected")
            return region
        if refused:
            names = "、".join(REGION_NAMES[region] for region, _ in refused)
            statuses = "、".join(sorted({str(status) for _, status in refused}))
            raise ASRCheckError(f"key 无效，{names}地域都不接受（HTTP {statuses}）", status=refused[-1][1])
        raise ASRCheckError(f"连不上阿里云百炼：{last_error!r}")

    async def _run_task(self, websocket: ClientConnection) -> str:
        task_id = uuid.uuid4().hex
        await websocket.send(
            json.dumps(
                {
                    "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                    "payload": {
                        "task_group": "audio",
                        "task": "asr",
                        "function": "recognition",
                        "model": self.model,
                        "parameters": dict(PARAMETERS),
                        "input": {},
                    },
                }
            )
        )
        async with asyncio.timeout(START_TIMEOUT):
            while True:
                message = await websocket.recv()
                header = _event(message).get("header", {})
                if header.get("event") == "task-started":
                    return task_id
                if header.get("event") == "task-failed":
                    raise ASRCheckError(f"启动不了 {self.model} 的识别任务（{_failure(header)}）")

    async def send_audio(self, pcm16: bytes) -> None:
        if not pcm16 or self._websocket is None or self._closing:
            return
        self._held.extend(pcm16)
        while len(self._held) >= PACKET_BYTES:
            packet = bytes(self._held[:PACKET_BYTES])
            del self._held[:PACKET_BYTES]
            await self._send(packet)

    async def _send(self, packet: bytes) -> None:
        websocket = self._websocket
        if websocket is None:
            return
        try:
            await websocket.send(packet)
            self._last_send = time.monotonic()
        except ConnectionClosed as exc:
            if not self._closing:
                self.on_error(ASRError(f"ASR 连接已断开（{_close_code(exc)}）"))
            raise

    async def _keepalive_loop(self) -> None:
        try:
            while not self._closing and self._websocket is not None:
                await asyncio.sleep(KEEPALIVE_TIMEOUT / 2)
                if time.monotonic() - self._last_send < KEEPALIVE_TIMEOUT:
                    continue
                if self._held:
                    packet = bytes(self._held).ljust(PACKET_BYTES, b"\0")
                    self._held.clear()
                    await self._send(packet)
                else:
                    await self._send(b"\0" * PACKET_BYTES)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                self.on_error(ASRError(f"ASR 保活失败：{exc}"))

    async def _receive_loop(self) -> None:
        websocket = self._websocket
        if websocket is None:
            return
        try:
            async for message in websocket:
                event = _event(message)
                header = event.get("header", {})
                kind = header.get("event")
                if kind == "result-generated":
                    sentence = event.get("payload", {}).get("output", {}).get("sentence")
                    if isinstance(sentence, dict):
                        self._heard(sentence)
                elif kind == "task-finished":
                    self._finished.set()
                    break
                elif kind == "task-failed":
                    self.on_error(ASRError(f"识别任务失败：{_failure(header)}"))
                    break
        except ConnectionClosed as exc:
            if not self._closing:
                self.on_error(ASRError(f"ASR 连接已断开（{_close_code(exc)}）"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                self.on_error(ASRError(f"ASR 接收失败：{exc}"))
        finally:
            self._connected = False
            if not self._closing:
                self.on_state("asr_closed")

    def _heard(self, sentence: dict[str, Any]) -> None:
        if sentence.get("heartbeat"):
            return
        text = str(sentence.get("text") or "")
        if sentence.get("sentence_end"):
            if _has_words(text):
                self.on_final(text)
            else:
                self.on_partial("")
        elif _has_words(text):
            self.on_partial(text)

    async def finish(self) -> None:
        websocket = self._websocket
        if websocket is None:
            return
        self._closing = True
        try:
            if self._held:
                packet = bytes(self._held).ljust(PACKET_BYTES, b"\0")
                self._held.clear()
                await websocket.send(packet)
            await websocket.send(
                json.dumps(
                    {
                        "header": {"action": "finish-task", "task_id": self._task_id, "streaming": "duplex"},
                        "payload": {"input": {}},
                    }
                )
            )
            try:
                async with asyncio.timeout(FINISH_TIMEOUT):
                    await self._finished.wait()
            except TimeoutError:
                pass
        except ConnectionClosed:
            pass
        finally:
            await self.close()

    async def close(self) -> None:
        self._closing = True
        for task in (self._keepalive_task, self._receive_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        self._keepalive_task = None
        self._receive_task = None
        websocket, self._websocket = self._websocket, None
        self._connected = False
        if websocket is not None:
            try:
                await websocket.close()
            except Exception:
                pass


def _event(message: str | bytes) -> dict[str, Any]:
    try:
        value = json.loads(message)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _failure(header: dict[str, Any]) -> str:
    return f"{header.get('error_code')}：{header.get('error_message')}"


def _close_code(exc: ConnectionClosed) -> str:
    received = exc.rcvd
    return str(received.code) if received is not None else "unknown"


def _has_words(text: str) -> bool:
    return any(char.isalnum() for char in text)


async def test_connection(api_key: str, model: str, *, proxy: str | None = None) -> str:
    """Start and immediately finish a task; returns the region that accepted the key."""
    asr = BailianASR(api_key, model, proxy=proxy)
    try:
        region = await asr.start()
        await asr.finish()
        return region
    finally:
        await asr.close()
