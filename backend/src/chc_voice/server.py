"""Local HTTP + WebSocket server for chc-voice.

Everything binds to 127.0.0.1.  The UI only ever receives masked key status; secrets are
read by the backend from the Keychain at the moment they are needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from .asr.bailian_stream import ASRCheckError, test_connection as test_asr
from .audio import coreaudio
from .audio.devices import (
    AudioDeviceError,
    default_input,
    default_output,
    ensure_virtual_mic,
    list_audio_devices,
    virtual_mic_status,
)
from .audio.output import play_tone
from .config import (
    DEFAULT_MODE,
    VALID_MODES,
    VALID_TONES,
    Settings,
    data_dir,
    ensure_runtime_files,
    load_settings,
    save_settings,
)
from .metrics import MetricsLog
from .relay.modes import ModeManager, ModeUnavailable
from .relay.pipeline import Pipeline
from .settings_store import SERVICES, Keychain, KeychainError, public_key_status
from .state import Mode, VoiceError
from .translate.translator import ChatTranslator
from .tts.fish_stream import FishTTS
from .tts.voices import VoiceCache, fetch_voices, filter_voices

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "webui"


class Runtime:
    def __init__(self, settings: Settings, keychain: Keychain) -> None:
        self.settings = settings
        self.keychain = keychain
        self.metrics = MetricsLog(data_dir() / "metrics.jsonl")
        self.pipeline = Pipeline(lambda: self.settings, keychain, emit=self._emit, metrics=self.metrics)
        self.clients: set[web.WebSocketResponse] = set()
        self.lock = asyncio.Lock()
        self.voice_cache = VoiceCache(data_dir() / "fish_voices.json")
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self) -> None:
        self._loop = asyncio.get_running_loop()

    def _emit(self, event: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self.broadcast(event)))
        except RuntimeError:
            pass

    async def broadcast(self, event: dict[str, Any]) -> None:
        if not self.clients:
            return
        message = json.dumps(event, ensure_ascii=False)
        stale: list[web.WebSocketResponse] = []
        for client in list(self.clients):
            try:
                await client.send_str(message)
            except Exception:
                stale.append(client)
        for client in stale:
            self.clients.discard(client)

    async def snapshot(self) -> dict[str, Any]:
        settings = self.settings
        manager = ModeManager(settings, self.keychain)
        try:
            devices = [device.as_dict() for device in list_audio_devices()]
        except AudioDeviceError as exc:
            devices = []
            device_error = str(exc)
        else:
            device_error = None
        try:
            default_in = default_input()
            default_out = default_output()
        except Exception:
            default_in = default_out = None
        return {
            "type": "snapshot",
            "status": self.pipeline.status(),
            "default_input": default_in.name if default_in else None,
            "default_output": default_out.name if default_out else None,
            "settings": public_settings(settings),
            "keys": public_key_status(self.keychain),
            "modes": [status.as_dict() for status in manager.statuses()],
            "devices": devices,
            "device_error": device_error,
            "virtual_mic": virtual_mic_status(settings.audio),
        }


def public_settings(settings: Settings) -> dict[str, Any]:
    """Settings safe for the UI: no keys, only masked status lives elsewhere."""
    return {
        "audio": {
            "input_device": settings.audio.input_device,
            "virtual_device": settings.audio.virtual_device,
            "blackhole_device": settings.audio.blackhole_device,
            "output_gain": settings.audio.output_gain,
            "monitor_gain": settings.audio.monitor_gain,
            "mic_gain": settings.audio.mic_gain,
            "monitor_enabled": settings.audio.monitor_enabled,
        },
        "translate": {"tone": settings.translation_tone},
        "api": {
            "llm_base_url": settings.api.llm_base_url,
            "llm_model": settings.api.llm_model,
            "bailian_model": settings.api.bailian_model,
        },
        "voices": {
            "fish_model": settings.voices.fish_model,
            "zh_voice_id": settings.voices.zh_voice_id,
            "zh_voice_name": settings.voices.zh_voice_name,
            "ja_voice_id": settings.voices.ja_voice_id,
            "ja_voice_name": settings.voices.ja_voice_name,
        },
        "ui": {
            "mode": settings.ui.mode,
            "show_transcript": settings.ui.show_transcript,
            "show_translation": settings.ui.show_translation,
        },
        "advanced": {
            "zh_direct": {
                "stable_ms": settings.commit_zh_direct.stable_ms,
                "min_chars": settings.commit_zh_direct.min_chars,
                "max_chars": settings.commit_zh_direct.max_chars,
                "pause_flush_ms": settings.commit_zh_direct.pause_flush_ms,
            },
            "zh_to_ja": {
                "stable_ms": settings.commit_zh_to_ja.stable_ms,
                "min_chars": settings.commit_zh_to_ja.min_chars,
                "max_chars": settings.commit_zh_to_ja.max_chars,
                "pause_flush_ms": settings.commit_zh_to_ja.pause_flush_ms,
            },
        },
        "network": {"fish_proxy": settings.network.fish_proxy},
    }


class Server:
    def __init__(self, runtime: Runtime, *, host: str = "127.0.0.1", port: int = 8791) -> None:
        self.runtime = runtime
        self.host = host
        self.port = port
        self.app = web.Application(client_max_size=2 * 1024 * 1024)
        self._setup_routes()

    @property
    def settings(self) -> Settings:
        return self.runtime.settings

    @property
    def pipeline(self) -> Pipeline:
        return self.runtime.pipeline

    @property
    def keychain(self) -> Keychain:
        return self.runtime.keychain

    def _setup_routes(self) -> None:
        app = self.app
        app.router.add_get("/", self.index)
        app.router.add_get("/app.js", self.static_js)
        app.router.add_get("/styles.css", self.static_css)
        app.router.add_get("/api/health", self.health)
        app.router.add_get("/api/state", self.state)
        app.router.add_get("/api/settings", self.get_settings)
        app.router.add_put("/api/settings/keys", self.put_key)
        app.router.add_delete("/api/settings/keys/{service}", self.delete_key)
        app.router.add_put("/api/settings/audio", self.put_audio)
        app.router.add_put("/api/settings/voices", self.put_voices)
        app.router.add_put("/api/settings/translate", self.put_translate)
        app.router.add_put("/api/settings/mode", self.put_mode)
        app.router.add_put("/api/settings/advanced", self.put_advanced)
        app.router.add_post("/api/settings/test/{service}", self.test_service)
        app.router.add_get("/api/devices", self.get_devices)
        app.router.add_post("/api/devices/refresh", self.get_devices)
        app.router.add_post("/api/virtual-mic/create", self.create_virtual_mic)
        app.router.add_get("/api/voices", self.get_voices)
        app.router.add_post("/api/start", self.start)
        app.router.add_post("/api/stop", self.stop)
        app.router.add_post("/api/panic", self.panic)
        app.router.add_post("/api/test-tone", self.test_tone)
        app.router.add_get("/ws", self.websocket)

    # ------------------------------------------------------------------ static
    async def index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def static_js(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "app.js")

    async def static_css(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "styles.css")

    # ------------------------------------------------------------------ observe
    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "version": "0.1.0", "state": self.pipeline.state.value})

    async def state(self, request: web.Request) -> web.Response:
        snapshot = await self.runtime.snapshot()
        return web.json_response({key: value for key, value in snapshot.items() if key != "type"})

    async def get_settings(self, request: web.Request) -> web.Response:
        snapshot = await self.runtime.snapshot()
        return web.json_response(
            {
                "settings": snapshot["settings"],
                "keys": snapshot["keys"],
                "modes": snapshot["modes"],
                "virtual_mic": snapshot["virtual_mic"],
            }
        )

    async def get_devices(self, request: web.Request) -> web.Response:
        try:
            devices = [device.as_dict() for device in list_audio_devices()]
        except AudioDeviceError as exc:
            return web.json_response({"ok": False, "message": str(exc)}, status=500)
        return web.json_response(
            {
                "ok": True,
                "devices": devices,
                "virtual_mic": virtual_mic_status(self.settings.audio),
            }
        )

    async def create_virtual_mic(self, request: web.Request) -> web.Response:
        status = await asyncio.to_thread(ensure_virtual_mic, self.settings.audio, create=True)
        code = 200 if status["ready"] else 409
        return web.json_response({"ok": status["ready"], "virtual_mic": status}, status=code)

    async def get_voices(self, request: web.Request) -> web.Response:
        language = request.query.get("language", "")
        cache = self.runtime.voice_cache
        voices = cache.load()
        error = None
        if cache.stale():
            key = self.keychain.get("tts")
            if key:
                try:
                    voices = await fetch_voices(
                        key, proxy=self.settings.network.fish_proxy or None, pages=3
                    )
                    cache.save(voices)
                except Exception as exc:
                    error = str(exc)
            else:
                error = "Fish TTS key 未配置"
        selected = filter_voices(voices, language) if language else voices
        return web.json_response(
            {
                "ok": error is None,
                "voices": [voice.as_dict() for voice in selected],
                "error": error,
                "cached": bool(voices),
            }
        )

    # ------------------------------------------------------------------ settings
    async def put_key(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        service = str(body.get("service") or "")
        value = str(body.get("key") or "")
        if service not in SERVICES:
            return web.json_response({"ok": False, "message": "未知服务"}, status=400)
        try:
            status = await asyncio.to_thread(self.keychain.set, service, value)
        except KeychainError as exc:
            return web.json_response({"ok": False, "message": str(exc)}, status=500)
        await self.runtime.broadcast(
            {"type": "keys", "keys": public_key_status(self.keychain)}
        )
        return web.json_response({"ok": True, "status": status.as_dict()})

    async def delete_key(self, request: web.Request) -> web.Response:
        service = request.match_info["service"]
        if service not in SERVICES:
            return web.json_response({"ok": False, "message": "未知服务"}, status=400)
        try:
            await asyncio.to_thread(self.keychain.delete, service)
        except KeychainError as exc:
            return web.json_response({"ok": False, "message": str(exc)}, status=500)
        await self.runtime.broadcast({"type": "keys", "keys": public_key_status(self.keychain)})
        return web.json_response({"ok": True, "keys": public_key_status(self.keychain)})

    async def put_audio(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        async with self.runtime.lock:
            settings = load_settings()
            audio = settings.audio
            for field in ("input_device", "virtual_device", "blackhole_device"):
                if field in body:
                    setattr(audio, field, str(body[field]))
            for field in ("output_gain", "monitor_gain", "mic_gain"):
                if field in body:
                    try:
                        setattr(audio, field, float(body[field]))
                    except (TypeError, ValueError):
                        return web.json_response({"ok": False, "message": f"{field} 不是数字"}, status=400)
            if "monitor_enabled" in body:
                audio.monitor_enabled = bool(body["monitor_enabled"])
            settings = settings.normalised()
            save_settings(settings)
            self.runtime.settings = settings
            self.pipeline.apply_settings(settings)
        return web.json_response(
            {
                "ok": True,
                "settings": public_settings(settings),
                "restart_required": self.pipeline.running,
                "message": "设备改动会在下一次开始后生效" if self.pipeline.running else "",
            }
        )

    async def put_voices(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        async with self.runtime.lock:
            settings = load_settings()
            voices = settings.voices
            for field in ("zh_voice_id", "zh_voice_name", "ja_voice_id", "ja_voice_name", "fish_model"):
                if field in body:
                    setattr(voices, field, str(body[field]).strip())
            settings = settings.normalised()
            save_settings(settings)
            self.runtime.settings = settings
        return web.json_response({"ok": True, "settings": public_settings(settings)})

    async def put_translate(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        tone = str(body.get("tone") or "").strip()
        if tone not in VALID_TONES:
            return web.json_response({"ok": False, "message": "未知语气"}, status=400)
        async with self.runtime.lock:
            settings = load_settings()
            settings.translation_tone = tone
            settings = settings.normalised()
            save_settings(settings)
            self.runtime.settings = settings
        return web.json_response({"ok": True, "settings": public_settings(settings)})

    async def put_mode(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        mode_text = str(body.get("mode") or "")
        try:
            mode = Mode.parse(mode_text)
        except ValueError as exc:
            return web.json_response({"ok": False, "message": str(exc)}, status=400)
        try:
            await self.pipeline.set_mode(mode)
        except ModeUnavailable as exc:
            return web.json_response(
                {"ok": False, "message": str(exc), "missing": exc.missing}, status=409
            )
        async with self.runtime.lock:
            settings = load_settings()
            settings.ui.mode = mode.value
            save_settings(settings)
            self.runtime.settings = settings
        await self.runtime.broadcast({"type": "mode", "mode": mode.value, "label": mode.label})
        return web.json_response({"ok": True, "mode": mode.value, "label": mode.label})

    async def put_advanced(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        async with self.runtime.lock:
            settings = load_settings()
            for mode_key, target in (
                ("zh_direct", settings.commit_zh_direct),
                ("zh_to_ja", settings.commit_zh_to_ja),
            ):
                payload = body.get(mode_key)
                if isinstance(payload, dict):
                    for field in ("stable_ms", "min_chars", "max_chars", "pause_flush_ms"):
                        if field in payload:
                            try:
                                setattr(target, field, int(payload[field]))
                            except (TypeError, ValueError):
                                return web.json_response(
                                    {"ok": False, "message": f"{mode_key}.{field} 不是整数"}, status=400
                                )
            ui = body.get("ui")
            if isinstance(ui, dict):
                for field in ("show_transcript", "show_translation"):
                    if field in ui:
                        setattr(settings.ui, field, bool(ui[field]))
            settings = settings.normalised()
            save_settings(settings)
            self.runtime.settings = settings
        return web.json_response(
            {
                "ok": True,
                "settings": public_settings(settings),
                "restart_required": self.pipeline.running,
            }
        )

    # ------------------------------------------------------------------ tests
    async def test_service(self, request: web.Request) -> web.Response:
        service = request.match_info["service"]
        body = await self._json(request)
        if service not in SERVICES:
            return web.json_response({"ok": False, "message": "未知服务"}, status=400)
        key = self.keychain.get(service)
        if not key:
            return web.json_response(
                {"ok": False, "service": SERVICES[service][2], "message": "key 未配置"}, status=400
            )
        try:
            if service == "asr":
                region = await test_asr(key, self.settings.api.bailian_model)
                return web.json_response({"ok": True, "detail": f"百炼连接正常（{region}）"})
            if service == "tts":
                voice = str(body.get("voice_id") or "").strip()
                if not voice:
                    voice, _ = self.settings.voices.voice_for(self.settings.ui.mode)
                if not voice:
                    return web.json_response(
                        {"ok": False, "service": "Fish TTS", "message": "没有配置音色"}, status=400
                    )
                tts = FishTTS(
                    key,
                    self.settings.voices.fish_model,
                    proxy=self.settings.network.fish_proxy or None,
                )
                total = await tts.test(voice)
                return web.json_response({"ok": True, "detail": f"Fish 合成正常（{total} 字节）"})
            if service == "llm":
                translator = ChatTranslator(
                    key,
                    self.settings.api.llm_base_url,
                    self.settings.api.llm_model,
                )
                translated = await translator.test()
                return web.json_response({"ok": True, "detail": f"翻译正常：{translated}"})
        except VoiceError as exc:
            return web.json_response(
                {"ok": False, "service": exc.service, "message": exc.message, "status": exc.status},
                status=502,
            )
        except Exception as exc:
            return web.json_response(
                {"ok": False, "service": SERVICES[service][2], "message": str(exc)}, status=502
            )
        return web.json_response({"ok": False, "message": "未知测试"}, status=400)

    # ------------------------------------------------------------------ control
    async def start(self, request: web.Request) -> web.Response:
        body = await self._json(request, allow_empty=True)
        mode: Mode | None = None
        if body.get("mode"):
            try:
                mode = Mode.parse(str(body["mode"]))
            except ValueError as exc:
                return web.json_response({"ok": False, "message": str(exc)}, status=400)
        try:
            await self.pipeline.start(mode)
        except ModeUnavailable as exc:
            return web.json_response(
                {"ok": False, "message": str(exc), "missing": exc.missing}, status=409
            )
        except VoiceError as exc:
            return web.json_response(
                {"ok": False, "service": exc.service, "message": exc.message, "status": exc.status},
                status=502,
            )
        except AudioDeviceError as exc:
            return web.json_response({"ok": False, "service": "音频设备", "message": str(exc)}, status=502)
        except Exception as exc:
            log.exception("start failed")
            return web.json_response({"ok": False, "message": str(exc)}, status=500)
        if mode is not None:
            async with self.runtime.lock:
                settings = load_settings()
                settings.ui.mode = mode.value
                save_settings(settings)
                self.runtime.settings = settings
        return web.json_response({"ok": True, "status": self.pipeline.status()})

    async def stop(self, request: web.Request) -> web.Response:
        await self.pipeline.stop()
        return web.json_response({"ok": True, "status": self.pipeline.status()})

    async def panic(self, request: web.Request) -> web.Response:
        await self.pipeline.panic()
        return web.json_response({"ok": True, "status": self.pipeline.status()})

    async def test_tone(self, request: web.Request) -> web.Response:
        body = await self._json(request, allow_empty=True)
        try:
            seconds = max(0.3, min(3.0, float(body.get("seconds", 1.2))))
        except (TypeError, ValueError):
            seconds = 1.2
        status = await asyncio.to_thread(ensure_virtual_mic, self.settings.audio, create=True)
        if not status["ready"]:
            return web.json_response(
                {"ok": False, "service": "虚拟麦克风", "message": status.get("guidance")}, status=409
            )
        if self.pipeline.running:
            self.pipeline.play_tone(seconds)
        else:
            await asyncio.to_thread(
                play_tone, self.settings.audio.blackhole_device, seconds=seconds
            )
        return web.json_response(
            {"ok": True, "message": f"已向 {status['consumer_name']} 写入 {seconds:.1f} 秒测试音"}
        )

    # ------------------------------------------------------------------ websocket
    async def websocket(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.runtime.clients.add(ws)
        try:
            await ws.send_json(await self.runtime.snapshot())
            async for message in ws:
                if message.type == WSMsgType.ERROR:
                    break
                if message.type == WSMsgType.TEXT:
                    # The UI uses REST for mutations; the socket is an event channel.
                    try:
                        payload = json.loads(message.data)
                    except ValueError:
                        continue
                    if payload.get("type") == "ping":
                        await ws.send_json({"type": "pong"})
        finally:
            self.runtime.clients.discard(ws)
        return ws

    # ------------------------------------------------------------------ helpers
    async def _json(self, request: web.Request, *, allow_empty: bool = False) -> dict[str, Any]:
        if allow_empty and request.content_length in (None, 0):
            return {}
        try:
            body = await request.json()
        except Exception:
            return {} if allow_empty else {}
        return body if isinstance(body, dict) else {}

    async def run(self) -> None:
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        log.info("chc-voice UI: http://%s:%s", self.host, self.port)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await runner.cleanup()


def create_app(
    *,
    data_directory: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8791,
) -> tuple[web.Application, Runtime, Server]:
    ensure_runtime_files(data_directory)
    settings = load_settings(data_directory)
    keychain = Keychain()
    runtime = Runtime(settings, keychain)
    server = Server(runtime, host=host, port=port)
    return server.app, runtime, server


async def serve(host: str = "127.0.0.1", port: int = 8791) -> None:
    app, runtime, server = create_app(host=host, port=port)
    runtime.bind_loop()
    await server.run()
