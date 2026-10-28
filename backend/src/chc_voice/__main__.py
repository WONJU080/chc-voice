"""Command line entry point: ``chc-voice serve`` plus P0 inspection commands."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .asr.bailian_stream import test_connection as test_asr
from .audio import coreaudio
from .audio.devices import ensure_virtual_mic, list_audio_devices, virtual_mic_status
from .audio.output import play_tone
from .config import ensure_runtime_files, log_path, load_settings
from .relay.pipeline import Pipeline
from .settings_store import SERVICES, Keychain, public_key_status
from .state import Mode, VoiceError
from .translate.translator import ChatTranslator
from .tts.fish_stream import FishTTS


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(formatter)
        root.addHandler(handler)
    except OSError:
        pass


def _settings():
    return ensure_runtime_files()


def cmd_devices(args: argparse.Namespace) -> int:
    settings = _settings()
    print("Core Audio:")
    try:
        for device in coreaudio.list_devices():
            print(f"  [{device.id}] {device.name}  in={device.inputs} out={device.outputs}  uid={device.uid}")
    except Exception as exc:
        print(f"  读取失败：{exc}")
    print("\nPortAudio:")
    try:
        for device in list_audio_devices():
            print(f"  [{device.index}] {device.name}  in={device.inputs} out={device.outputs}  {device.samplerate:.0f}Hz")
    except Exception as exc:
        print(f"  读取失败：{exc}")
    status = virtual_mic_status(settings.audio)
    print("\n虚拟麦克风:")
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if status["ready"] else 1


def cmd_aggregate(args: argparse.Namespace) -> int:
    if args.action == "create":
        device = coreaudio.create_aggregate(name=args.name, sub_device_name=args.sub, replace=args.replace)
        print(json.dumps({"id": device.id, "name": device.name, "uid": device.uid, "inputs": device.inputs, "outputs": device.outputs}, ensure_ascii=False, indent=2))
    elif args.action == "destroy":
        print(json.dumps({"destroyed": coreaudio.destroy_aggregate(args.name)}))
    else:
        print(json.dumps(coreaudio.status(args.name, args.sub), ensure_ascii=False, indent=2))
    return 0


def cmd_tone(args: argparse.Namespace) -> int:
    settings = _settings()
    status = ensure_virtual_mic(settings.audio, create=True)
    if not status["ready"]:
        print(status.get("guidance") or "虚拟麦克风不可用", file=sys.stderr)
        return 1
    print(f"向 {status['consumer_name']} 写入 {args.seconds:.1f} 秒 {args.frequency:.0f}Hz 测试音…")
    play_tone(settings.audio.blackhole_device, frequency=args.frequency, seconds=args.seconds)
    print("完成。请在语音备忘录/微信/QuickTime 里选择 chc-voice 录音验证。")
    return 0


def cmd_test_asr(args: argparse.Namespace) -> int:
    settings = _settings()
    key = Keychain().get("asr")
    if not key:
        print("百炼 ASR key 未配置", file=sys.stderr)
        return 1
    started = time.monotonic()
    try:
        region = asyncio.run(test_asr(key, settings.api.bailian_model))
    except VoiceError as exc:
        print(f"{exc.service}: {exc.message}", file=sys.stderr)
        return 1
    print(f"ASR 正常：{region}，{int((time.monotonic() - started) * 1000)} ms")
    return 0


def cmd_test_llm(args: argparse.Namespace) -> int:
    settings = _settings()
    key = Keychain().get("llm")
    if not key:
        print("翻译 LLM key 未配置", file=sys.stderr)
        return 1
    translator = ChatTranslator(key, settings.api.llm_base_url, settings.api.llm_model)
    try:
        translated = asyncio.run(translator.test())
    except VoiceError as exc:
        print(f"{exc.service}: {exc.message}", file=sys.stderr)
        return 1
    print(f"翻译正常：{translated}")
    return 0


def cmd_test_tts(args: argparse.Namespace) -> int:
    settings = _settings()
    key = Keychain().get("tts")
    if not key:
        print("Fish TTS key 未配置", file=sys.stderr)
        return 1
    voice = args.voice or settings.voices.voice_for(args.mode)[0]
    if not voice:
        print("没有配置音色", file=sys.stderr)
        return 1
    tts = FishTTS(key, settings.voices.fish_model, proxy=settings.network.fish_proxy or None)
    started = time.monotonic()
    try:
        total = asyncio.run(tts.test(voice, args.text))
    except VoiceError as exc:
        print(f"{exc.service}: {exc.message}", file=sys.stderr)
        return 1
    print(f"Fish 正常：{total} 字节，{int((time.monotonic() - started) * 1000)} ms")
    return 0


def cmd_key(args: argparse.Namespace) -> int:
    keychain = Keychain()
    if args.action == "status":
        print(json.dumps(public_key_status(keychain), ensure_ascii=False, indent=2))
        return 0
    if args.service not in SERVICES:
        print(f"未知服务：{args.service}；可用：{', '.join(SERVICES)}", file=sys.stderr)
        return 2
    if args.action == "set":
        value = sys.stdin.read().strip() if args.stdin else getpass.getpass(f"{SERVICES[args.service][2]} key: ")
        if not value:
            print("key 为空", file=sys.stderr)
            return 1
        status = keychain.set(args.service, value)
        print(f"已保存：{status.service} ····{status.last4}")
        return 0
    if args.action == "delete":
        keychain.delete(args.service)
        print("已删除")
        return 0
    if args.action == "import-env":
        path = Path(args.service or args.path)
        imported = keychain.load_env_file(path)
        print(f"从 {path} 导入 {imported} 个 key 到 Keychain")
        return 0 if imported else 1
    return 2


def cmd_voices(args: argparse.Namespace) -> int:
    from .tts.voices import VoiceCache, fetch_voices, filter_voices
    from .config import data_dir

    settings = _settings()
    cache = VoiceCache(data_dir() / "fish_voices.json")
    voices = cache.load()
    if args.refresh or cache.stale():
        key = Keychain().get("tts")
        if not key:
            print("Fish TTS key 未配置", file=sys.stderr)
            return 1
        voices = asyncio.run(fetch_voices(key, proxy=settings.network.fish_proxy or None))
        cache.save(voices)
    if args.language:
        voices = filter_voices(voices, args.language)
    for voice in voices:
        print(f"{voice.id}\t{voice.title}\t{'/'.join(voice.languages)}")
    return 0


async def cmd_listen(args: argparse.Namespace) -> int:
    settings = _settings()
    keychain = Keychain()

    def emit(event: dict) -> None:
        if event.get("type") in ("level",):
            return
        print(json.dumps(event, ensure_ascii=False), flush=True)

    pipeline = Pipeline(lambda: settings, keychain, emit=emit)
    mode = Mode.parse(args.mode)
    try:
        await pipeline.start(mode)
    except Exception as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    try:
        if args.seconds:
            await asyncio.sleep(args.seconds)
        else:
            print("聆听中，Ctrl+C 停止…", file=sys.stderr)
            while True:
                await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await pipeline.stop()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from aiohttp import web

    from .server import Runtime, Server

    setup_logging(args.verbose)
    settings = ensure_runtime_files()
    keychain = Keychain()
    runtime = Runtime(settings, keychain)
    server = Server(runtime, host=args.host, port=args.port)

    async def run() -> None:
        runtime.bind_loop()
        runner = web.AppRunner(server.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, args.host, args.port)
        await site.start()
        print(f"chc-voice 已启动：http://{args.host}:{args.port}", flush=True)
        if args.open:
            import subprocess

            subprocess.Popen(["open", f"http://{args.host}:{args.port}"])
        try:
            await asyncio.Event().wait()
        finally:
            await pipeline_shutdown(runtime)
            await runner.cleanup()

    async def pipeline_shutdown(rt: Runtime) -> None:
        await rt.pipeline.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chc-voice", description="macOS local speech relay")
    parser.add_argument("--version", action="version", version=f"chc-voice {__version__}")
    sub = parser.add_subparsers(dest="command")

    devices = sub.add_parser("devices", help="列出音频设备并检查虚拟麦克风")
    devices.set_defaults(func=cmd_devices)

    aggregate = sub.add_parser("aggregate", help="管理 chc-voice Aggregate Device")
    aggregate.add_argument("action", choices=["status", "create", "destroy"])
    aggregate.add_argument("--name", default="chc-voice")
    aggregate.add_argument("--sub", default="BlackHole 2ch")
    aggregate.add_argument("--replace", action="store_true")
    aggregate.set_defaults(func=cmd_aggregate)

    tone = sub.add_parser("tone", help="向虚拟麦克风写入测试音")
    tone.add_argument("--seconds", type=float, default=1.5)
    tone.add_argument("--frequency", type=float, default=440.0)
    tone.set_defaults(func=cmd_tone)

    test_asr_parser = sub.add_parser("test-asr", help="测试百炼 ASR key")
    test_asr_parser.set_defaults(func=cmd_test_asr)

    test_llm_parser = sub.add_parser("test-llm", help="测试翻译 LLM key")
    test_llm_parser.set_defaults(func=cmd_test_llm)

    test_tts_parser = sub.add_parser("test-tts", help="测试 Fish TTS key")
    test_tts_parser.add_argument("--voice", default="")
    test_tts_parser.add_argument("--mode", default="zh_direct", choices=["zh_direct", "zh_to_ja"])
    test_tts_parser.add_argument("--text", default="你好，这是一段测试。")
    test_tts_parser.set_defaults(func=cmd_test_tts)

    key = sub.add_parser("key", help="Keychain 中的 API key")
    key.add_argument("action", choices=["status", "set", "delete", "import-env"])
    key.add_argument("service", nargs="?", default="")
    key.add_argument("path", nargs="?", default=".env")
    key.add_argument("--stdin", action="store_true", help="从标准输入读取 key（默认交互式）")
    key.set_defaults(func=cmd_key)

    voices = sub.add_parser("voices", help="列出 Fish Audio 音色")
    voices.add_argument("--language", default="")
    voices.add_argument("--refresh", action="store_true")
    voices.set_defaults(func=cmd_voices)

    listen = sub.add_parser("listen", help="不开 UI，直接跑模式 A/B（P0）")
    listen.add_argument("--mode", default="zh_direct", choices=["zh_direct", "zh_to_ja"])
    listen.add_argument("--seconds", type=float, default=0)
    listen.set_defaults(func=lambda args: asyncio.run(cmd_listen(args)))

    serve = sub.add_parser("serve", help="启动本地 UI 服务（默认）")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8791)
    serve.add_argument("--open", action="store_true")
    serve.add_argument("--verbose", action="store_true")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args = parser.parse_args(["serve"])
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
