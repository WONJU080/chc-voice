"""The relay pipeline: microphone -> ASR -> stable prefix -> [translate] -> TTS -> mic.

State machine::

    idle -> starting -> listening -> recognizing -> [translating] -> synthesizing
         -> speaking -> listening ... -> stopping -> idle
    any error -> error (virtual microphone is silenced, UI gets the service name)
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable

from ..asr.bailian_stream import ASRError, BailianASR
from ..audio.capture import MicrophoneCapture
from ..audio.devices import AudioDeviceError, ensure_virtual_mic, virtual_mic_status
from ..audio.monitor import Monitor
from ..audio.virtual_mic import VirtualMic
from ..config import Settings, metrics_path
from ..metrics import MetricsLog, SegmentMetrics
from ..settings_store import Keychain
from ..state import Commit, Mode, State, VoiceError
from ..text.stable_prefix import CommitRule, StablePrefixCommitter
from ..translate.translator import ChatTranslator, TranslationError
from ..tts.fish_http import synthesize_http
from ..tts.fish_stream import FishError, FishTTS
from .cancel import Generation
from .modes import ModeManager, ModeUnavailable
from .queue import SegmentQueue

log = logging.getLogger(__name__)

SCRATCH = 0.08  # seconds of buffered audio to leave before declaring playback done


def _digits_only(text: str) -> bool:
    return bool(text) and all(char.isdigit() or char in " .,，。、!?！？" for char in text)


def _has_han(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


class Pipeline:
    def __init__(
        self,
        settings_provider: Callable[[], Settings],
        keychain: Keychain,
        *,
        emit: Callable[[dict[str, Any]], None] | None = None,
        metrics: MetricsLog | None = None,
    ) -> None:
        self._settings_provider = settings_provider
        self._keychain = keychain
        self._emit_fn = emit or (lambda _event: None)
        self._metrics = metrics or MetricsLog(metrics_path())
        self._lock = asyncio.Lock()
        initial = settings_provider()
        self._mode = Mode.parse(initial.ui.mode)
        self._state = State.IDLE
        self._running = False
        self._stopping = False
        self._worker_busy = False
        self._last_error: dict[str, Any] | None = None
        self._virtual: VirtualMic | None = None
        self._monitor: Monitor | None = None
        self._capture: MicrophoneCapture | None = None
        self._asr: BailianASR | None = None
        self._committer: StablePrefixCommitter | None = None
        self._tts_client: FishTTS | None = None
        self._tts_voice: str | None = None
        self._queue = SegmentQueue[Commit]()
        self._session = Generation()
        self._work = Generation()
        self._tasks: set[asyncio.Task] = set()
        self._capture_task: asyncio.Task | None = None
        self._ticker_task: asyncio.Task | None = None
        self._worker_task: asyncio.Task | None = None
        self._fail_task: asyncio.Task | None = None
        self._input_level = -120.0
        self._output_level = -120.0
        self._asr_region: str | None = None
        self._last_partial = ""
        self._current_text = ""
        self._feedback_since: float | None = None

    # ------------------------------------------------------------------ observe
    @property
    def state(self) -> State:
        return self._state

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> dict[str, Any] | None:
        return self._last_error

    def settings(self) -> Settings:
        return self._settings_provider()

    def status(self) -> dict[str, Any]:
        settings = self.settings()
        return {
            "state": self._state.value,
            "state_label": self._state.label,
            "mode": self._mode.value,
            "mode_label": self._mode.label,
            "running": self._running,
            "input_level_dbfs": round(self._input_level, 1),
            "output_level_dbfs": round(self._output_level, 1),
            "queue": self._queue.size,
            "asr_region": self._asr_region,
            "last_error": self._last_error,
            "mic": self._capture.health() if self._capture else None,
            "virtual_mic": self._virtual.health() if self._virtual else virtual_mic_status(settings.audio),
            "monitor": self._monitor.health() if self._monitor else {"enabled": settings.audio.monitor_enabled},
        }

    # ------------------------------------------------------------------ lifecycle
    async def start(self, mode: Mode | None = None) -> None:
        async with self._lock:
            if self._running:
                return
            settings = self.settings()
            # BlackHole is installed but the aggregate may not have been created yet;
            # creating an aggregate needs no administrator rights.
            if virtual_mic_status(settings.audio)["blackhole"]:
                await asyncio.to_thread(ensure_virtual_mic, settings.audio, create=True)
            manager = ModeManager(settings, self._keychain)
            target = mode or self._mode
            manager.validate(target)
            self._mode = target
            self._last_error = None
            self._set_state(State.STARTING)
            try:
                key_asr = self._keychain.get("asr")
                if not key_asr:
                    raise ModeUnavailable(target, ["百炼 ASR key 未配置"])
                self._virtual = VirtualMic(settings.audio)
                self._virtual.start()
                self._monitor = Monitor(settings.audio)
                try:
                    self._monitor.start()
                except Exception as exc:
                    self._monitor.set_enabled(False)
                    self._emit("warning", message=f"监听打开失败：{exc}")
                rule = self._rule(settings, target)
                self._committer = StablePrefixCommitter(rule)
                key_tts = self._keychain.get("tts")
                if key_tts:
                    self._tts_client = FishTTS(
                        key_tts,
                        settings.voices.fish_model,
                        proxy=settings.network.fish_proxy or None,
                    )
                    voice, _ = settings.voices.voice_for(target.value)
                    if voice:
                        self._tts_client.prewarm(voice)
                        self._tts_voice = voice
                self._asr = BailianASR(
                    key_asr,
                    settings.api.bailian_model,
                    on_partial=self._on_partial,
                    on_final=self._on_final,
                    on_error=self._on_asr_error,
                    on_state=self._on_asr_state,
                )
                await self._asr.start()
                self._asr_region = self._asr.region
                self._capture = MicrophoneCapture(settings.audio)
                await self._capture.start()
                self._running = True
                self._stopping = False
                session = self._session.bump()
                work = self._work.bump()
                self._queue = SegmentQueue()
                self._capture_task = self._spawn(self._capture_loop(session), "capture")
                self._ticker_task = self._spawn(self._ticker(session), "ticker")
                self._worker_task = self._spawn(self._worker(work), "worker")
                self._set_state(State.LISTENING)
                self._metrics.startup(self._mode.value, device=self._capture.device_name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._shutdown()
                error = exc if isinstance(exc, VoiceError) else VoiceError("chc-voice", str(exc))
                self._record_error(error)
                self._set_state(State.ERROR, error.message)
                raise

    async def stop(self) -> None:
        async with self._lock:
            if not self._running and self._state is State.IDLE:
                return
            self._set_state(State.STOPPING)
            await self._shutdown()
            self._set_state(State.IDLE)

    async def panic(self) -> None:
        if self._virtual is not None:
            self._virtual.mute()
        await self.stop()
        self._set_state(State.IDLE, "已紧急静音")

    async def set_mode(self, mode: Mode) -> None:
        settings = self.settings()
        manager = ModeManager(settings, self._keychain)
        manager.validate(mode)
        self._mode = mode
        settings.ui.mode = mode.value
        self._emit("mode", mode=mode.value, label=mode.label)
        if not self._running:
            return
        self._work.bump()
        old_worker, self._worker_task = self._worker_task, None
        self._queue.clear()
        self._queue = SegmentQueue()
        self._worker_busy = False
        if old_worker is not None:
            old_worker.cancel()
            try:
                await old_worker
            except (asyncio.CancelledError, Exception):
                pass
        if self._virtual is not None:
            self._virtual.clear()
        if self._monitor is not None:
            self._monitor.clear()
        self._committer = StablePrefixCommitter(self._rule(settings, mode))
        if self._tts_client is not None:
            try:
                await self._tts_client.close()
            except Exception:
                pass
            self._tts_client = None
        key_tts = self._keychain.get("tts")
        if key_tts:
            self._tts_client = FishTTS(
                key_tts,
                settings.voices.fish_model,
                proxy=settings.network.fish_proxy or None,
            )
            voice, _ = settings.voices.voice_for(mode.value)
            if voice:
                self._tts_client.prewarm(voice)
                self._tts_voice = voice
        self._worker_task = self._spawn(self._worker(self._work.value), "worker")
        self._set_state(State.LISTENING)

    def play_tone(self, seconds: float = 1.2) -> None:
        """Write a test tone into the virtual microphone (or BlackHole when idle)."""
        from ..audio.output import play_tone as blocking_tone

        if self._virtual is not None and self._running:
            self._virtual.write_tone(880.0, seconds)
            return
        blocking_tone(self.settings().audio.blackhole_device, seconds=seconds)

    def apply_settings(self, settings: Settings) -> None:
        if self._capture is not None:
            # Capture reads mic_gain from this object on every callback.
            self._capture.settings = settings.audio
        if self._virtual is not None:
            self._virtual.set_gain(settings.audio.output_gain)
        if self._monitor is not None:
            self._monitor.set_gain(settings.audio.monitor_gain)
            try:
                self._monitor.set_enabled(settings.audio.monitor_enabled)
            except Exception as exc:
                self._emit("warning", message=f"监听打开失败：{exc}")

    # ------------------------------------------------------------------ ASR callbacks
    def _on_partial(self, text: str) -> None:
        if not self._running:
            return
        if text != self._last_partial:
            self._last_partial = text
            self._emit("transcript", text=text, final=False)
        if not self._worker_busy and self._state in (State.LISTENING, State.RECOGNIZING):
            self._set_state(State.RECOGNIZING)
        if self._committer is None:
            return
        commits = self._committer.feed(text)
        self._enqueue(commits)

    def _on_final(self, text: str) -> None:
        if not self._running:
            return
        last_partial = self._last_partial
        self._last_partial = ""
        self._emit("transcript", text=text, final=True)
        if self._committer is None:
            return
        # ASR often normalizes spoken digits ("一二三") to "123" in the final result.
        # If the partial still has Chinese numerals, keep that spoken form so the
        # translator/TTS read it as the user said it instead of as a cardinal number.
        spoken = text
        if _digits_only(text) and _has_han(last_partial):
            spoken = last_partial
        commits = self._committer.feed(spoken, final=True)
        self._enqueue(commits)
        if not self._worker_busy and self._queue.empty():
            self._set_state(State.LISTENING)

    def _on_asr_error(self, exc: Exception) -> None:
        if self._running:
            self._schedule_fail(exc)

    def _on_asr_state(self, state: str) -> None:
        self._emit("asr", status=state, region=self._asr_region)
        if state == "asr_closed" and self._running and not self._stopping:
            self._schedule_fail(ASRError("ASR 连接意外关闭"))

    # ------------------------------------------------------------------ loops
    async def _capture_loop(self, session: int) -> None:
        try:
            capture = self._capture
            asr = self._asr
            if capture is None or asr is None:
                return
            async for frame in capture.frames():
                if not self._running or not self._session.current(session):
                    break
                self._input_level = frame.level_dbfs
                await asr.send_audio(frame.pcm16)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._running and not self._stopping:
                self._schedule_fail(exc)

    async def _ticker(self, session: int) -> None:
        last_level = 0.0
        try:
            while self._running and self._session.current(session):
                await asyncio.sleep(0.05)
                now = time.monotonic()
                if self._committer is not None:
                    self._enqueue(self._committer.tick(now=now))
                self._output_level = self._virtual.level_dbfs if self._virtual else -120.0
                if self._capture is not None and self._capture.last_frame_at and now - self._capture.last_frame_at > 2.5:
                    self._schedule_fail(AudioDeviceError("麦克风停止提供音频帧（设备可能已断开）"))
                    return
                if self._virtual is not None and self._virtual.error:
                    self._schedule_fail(AudioDeviceError(self._virtual.error))
                    return
                self._check_feedback(now)
                if now - last_level >= 0.1:
                    last_level = now
                    self._emit(
                        "level",
                        input=round(self._input_level, 1),
                        output=round(self._output_level, 1),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._running and not self._stopping:
                self._schedule_fail(exc)

    async def _worker(self, work: int) -> None:
        try:
            while self._running and self._work.current(work):
                commit = await self._queue.get()
                if commit is None:
                    break
                if not self._work.current(work):
                    continue
                await self._speak(commit, work)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._running and not self._stopping:
                self._schedule_fail(exc)

    async def _speak(self, commit: Commit, work: int) -> None:
        settings = self.settings()
        if not commit.text.strip():
            return
        self._worker_busy = True
        self._current_text = commit.text
        spoken = commit.text
        translation_ms: float | None = None
        voice_id, voice_name = settings.voices.voice_for(self._mode.value)
        if not voice_id:
            self._worker_busy = False
            raise VoiceError("Fish TTS", "没有配置当前模式的 Fish 音色")
        try:
            if self._tts_client is not None:
                self._tts_client.prewarm(voice_id)
            if self._mode is Mode.ZH_TO_JA:
                self._set_state(State.TRANSLATING)
                key = self._keychain.get("llm")
                if not key:
                    raise TranslationError("翻译 LLM key 未配置")
                translator = ChatTranslator(
                    key,
                    settings.api.llm_base_url,
                    settings.api.llm_model,
                    tone=settings.translation_tone,
                )
                started = time.monotonic()
                spoken = await translator.translate(commit.text)
                translation_ms = (time.monotonic() - started) * 1000.0
                self._emit("translation", source=commit.text, text=spoken)
                self._metrics.write("translation", ms=round(translation_ms), chars=len(spoken))

            self._set_state(State.SYNTHESIZING)
            key = self._keychain.get("tts")
            if not key:
                raise VoiceError("Fish TTS", "Fish TTS key 未配置")
            tts = self._tts_client
            if tts is None:
                tts = FishTTS(
                    key,
                    settings.voices.fish_model,
                    proxy=settings.network.fish_proxy or None,
                )
                self._tts_client = tts
                tts.prewarm(voice_id)
            first_audio: float | None = None
            total_bytes = 0

            async def consume(chunks: Any) -> tuple[float | None, int]:
                nonlocal first_audio, total_bytes
                try:
                    async for chunk in chunks:
                        if not self._work.current(work) or not self._running:
                            break
                        if first_audio is None:
                            first_audio = time.monotonic()
                            self._set_state(State.SPEAKING, f"音色：{voice_name}" if voice_name else "")
                        total_bytes += len(chunk)
                        if self._virtual is not None:
                            self._virtual.write_pcm(chunk, tts.sample_rate)
                        if self._monitor is not None:
                            self._monitor.write_pcm(chunk, tts.sample_rate)
                finally:
                    close = getattr(chunks, "aclose", None)
                    if close is not None:
                        try:
                            await close()
                        except Exception:
                            pass
                return first_audio, total_bytes

            generator = tts.synthesize(spoken, voice_id)
            try:
                await consume(generator)
            except FishError:
                if total_bytes:
                    raise
                # WebSocket failed before any audio: try the HTTP fallback once.
                self._metrics.write("tts_fallback", reason="websocket")
                await consume(
                    synthesize_http(
                        key,
                        settings.voices.fish_model,
                        spoken,
                        voice_id,
                        proxy=settings.network.fish_proxy or None,
                        sample_rate=tts.sample_rate,
                    )
                )
            if first_audio is None or total_bytes == 0:
                raise FishError("Fish Audio 没有返回音频")

            # Let the buffer drain before claiming we are listening again; if another
            # commit is already queued, the worker starts it immediately instead.
            if self._queue.empty():
                deadline = time.monotonic() + max(3.0, len(spoken) / 5.0)
                while (
                    self._running
                    and self._work.current(work)
                    and self._virtual is not None
                    and self._virtual.pending_seconds > SCRATCH
                    and time.monotonic() < deadline
                ):
                    await asyncio.sleep(0.02)

            metrics = SegmentMetrics(
                mode=self._mode.value,
                commit_at=commit.at,
                translation_ms=translation_ms,
                tts_first_audio_ms=(first_audio - commit.at) * 1000.0,
                total_ms=(time.monotonic() - commit.at) * 1000.0,
                chars=len(spoken),
                source_chars=len(commit.text),
            )
            self._metrics.segment(metrics)
            self._emit("segment", **metrics.as_dict())
            self._worker_busy = False
            if self._running and self._queue.empty() and self._work.current(work):
                self._set_state(State.LISTENING)
        except asyncio.CancelledError:
            self._worker_busy = False
            raise
        except Exception:
            self._worker_busy = False
            raise

    # ------------------------------------------------------------------ failures
    def _schedule_fail(self, exc: Exception) -> None:
        if not self._running or self._stopping:
            return
        if self._fail_task is not None and not self._fail_task.done():
            return
        self._fail_task = asyncio.create_task(self._fail(exc), name="pipeline-fail")

    async def _fail(self, exc: Exception) -> None:
        if self._stopping:
            return
        self._stopping = True
        error = exc if isinstance(exc, VoiceError) else VoiceError("chc-voice", str(exc))
        self._record_error(error)
        self._set_state(State.ERROR, error.message)
        if self._virtual is not None:
            self._virtual.clear()
        await self._shutdown()
        self._stopping = False

    def _record_error(self, error: VoiceError) -> None:
        self._last_error = {
            "service": error.service,
            "message": error.message,
            "status": error.status,
        }
        self._metrics.error(error.service, error.message, error.status)
        self._emit("error", service=error.service, message=error.message, status=error.status)

    async def _shutdown(self) -> None:
        was_running = self._running
        self._running = False
        self._session.bump()
        self._work.bump()
        tasks = [self._capture_task, self._ticker_task, self._worker_task]
        self._capture_task = self._ticker_task = self._worker_task = None
        for task in tasks:
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        for task in tasks:
            if task is not None and task is not asyncio.current_task():
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._queue.clear()
        if self._capture is not None:
            try:
                await self._capture.stop()
            except Exception:
                pass
            self._capture = None
        if self._asr is not None:
            try:
                await asyncio.wait_for(self._asr.finish(), 3.0)
            except Exception:
                pass
            try:
                await self._asr.close()
            except Exception:
                pass
            self._asr = None
        if self._tts_client is not None:
            try:
                await self._tts_client.close()
            except Exception:
                pass
            self._tts_client = None
        if self._virtual is not None:
            try:
                self._virtual.stop()
            except Exception:
                pass
            self._virtual = None
        if self._monitor is not None:
            try:
                self._monitor.stop()
            except Exception:
                pass
            self._monitor = None
        self._output_level = -120.0
        self._worker_busy = False
        if was_running:
            self._metrics.shutdown()

    # ------------------------------------------------------------------ helpers
    def _check_feedback(self, now: float) -> None:
        """Mute if headphone monitoring is on and the microphone hears the output."""
        if self._monitor is None or not self._monitor.enabled:
            self._feedback_since = None
            return
        if self._output_level > -20.0 and self._input_level > -20.0:
            if self._feedback_since is None:
                self._feedback_since = now
            elif now - self._feedback_since > 1.5:
                self._feedback_since = None
                self._schedule_fail(VoiceError("反馈保护", "检测到监听反馈，已静音；请戴耳机或关闭监听"))
        else:
            self._feedback_since = None

    def _enqueue(self, commits: list[Commit]) -> None:
        for commit in commits:
            self._emit("commit", text=commit.text, reason=commit.reason, mode=self._mode.value)
            if not self._queue.put(commit):
                self._emit("warning", message="音频队列已满，丢弃了一段文字")

    def _rule(self, settings: Settings, mode: Mode) -> CommitRule:
        commit = settings.commit_for(mode.value)
        return CommitRule(
            stable_ms=commit.stable_ms,
            min_chars=commit.min_chars,
            max_chars=commit.max_chars,
            pause_flush_ms=commit.pause_flush_ms,
        )

    def _spawn(self, coro: Any, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=f"chc-{name}")
        self._tasks.add(task)

        def done(completed: asyncio.Task) -> None:
            self._tasks.discard(completed)
            if completed.cancelled():
                return
            exc = completed.exception()
            if exc is not None and self._running and not self._stopping:
                self._schedule_fail(exc)

        task.add_done_callback(done)
        return task

    def _set_state(self, state: State, detail: str = "") -> None:
        self._state = state
        self._emit(
            "state",
            state=state.value,
            label=state.label,
            mode=self._mode.value,
            detail=detail,
        )

    def _emit(self, kind: str, **fields: Any) -> None:
        payload = {"type": kind, **fields}
        try:
            self._emit_fn(payload)
        except Exception:
            pass
