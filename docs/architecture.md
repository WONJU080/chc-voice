# 架构

## 进程与端口

```text
Electron (app/main.js)
  └─ 选空闲端口 → 启动 .venv/bin/chc-voice serve
       └─ aiohttp，只监听 127.0.0.1:<port>
            ├─ GET  /              Web UI
            ├─ REST /api/*         设置、设备、测试、开始/停止
            └─ WS   /ws            状态、电平、识别、翻译、延迟事件
```

UI 不直接拿 key，也不直接碰音频设备。浏览器面板和 Electron 壳使用同一套页面；Electron 只负责启动后端和窗口生命周期。

## 后端模块

| 模块 | 职责 |
|---|---|
| `audio/capture.py` | 真实麦克风，16 kHz 单声道，20 ms 帧；失败回退设备原生采样率并重采样 |
| `audio/resample.py` | 多相重采样（SciPy），无 SciPy 时线性回退 |
| `audio/output.py` | 48 kHz 双声道连续输出，环形缓冲，空缓冲写零，限幅，电平 |
| `audio/virtual_mic.py` | 写 BlackHole 2ch；Aggregate `chc-voice` 供其他软件选择 |
| `audio/monitor.py` | 可选耳机监听；独立 sink，绝不混入虚拟麦克风 |
| `audio/coreaudio.py` | ctypes 调 Core Audio：设备枚举、UID、创建/销毁 Aggregate Device |
| `asr/bailian_stream.py` | DashScope duplex ASR：`run-task` → 音频 → `result-generated` → `finish-task` |
| `text/stable_prefix.py` | 稳定前缀 + 提交规则；不撤回已提交文本 |
| `text/segmenter.py` | 过长文本按标点/词边界切分 |
| `translate/translator.py` | OpenAI 兼容 Chat Completions；只做翻译；`tone` 控制口语/普通体/礼貌/敬语 |
| `tts/fish_stream.py` | Fish live WebSocket + 连接预热 |
| `tts/fish_http.py` | WebSocket 在首块音频前失败时的 HTTP 回退 |
| `relay/pipeline.py` | 状态机、任务编排、取消、错误静音 |
| `relay/modes.py` | 模式可用性检查（key、音色、虚拟麦克风） |
| `settings_store.py` | Keychain（`security` CLI）；只返回掩码状态 |
| `server.py` | REST/WS、静态 UI、测试接口 |

## 数据流

```text
Capture (16k mono, 20ms)
  → BailianASR (100ms packets)
  → partial/final
  → StablePrefixCommitter
  → SegmentQueue
  → Worker
      → [mode B] ChatTranslator (中→日)
      → FishTTS.synthesize (PCM 24k)
      → VirtualMic.write_pcm (resample 24k→48k, stereo)
      → Monitor.write_pcm (optional)
```

ASR partial 是累积文本。稳定前缀取最近 `stable_ms` 窗口内所有 partial 的最长公共前缀；提交规则：

1. 标点（句号/问号/逗号等）且达到阈值；
2. ASR `sentence_end`（final）提交剩余全文；
3. 超过 `max_chars` 强制切分；
4. `pause_flush_ms > 0` 时才在停顿处切分。默认 0：宁可多等，不把词切成两半。

## 状态机

```text
idle → starting → listening
listening ⇄ recognizing
listening/recognizing → translating → synthesizing → speaking → listening
任意状态 → error（写静音、报服务名）
任意状态 → stopping → idle
```

- `error` 后可以再次“开始”重试；
- 任何错误都先 `virtual.clear()`，再关闭输出；输出端在清空后的下一次回调立即写零；
- 停止时清空环形缓冲，继续写约 120 ms 静音后关闭流，保证其他软件不会收到残余音频；
- 监听开启时检测输入/输出同时过高的持续反馈，1.5 秒后自动静音并报“反馈保护”。

## 并发模型

- 一个 asyncio 事件循环；
- PortAudio 回调在音频线程，只做增益、重采样、入队；通过 `loop.call_soon_threadsafe` 交给事件循环；
- ASR 接收、TTS 接收、ticker、worker 都是独立 task；
- `Generation` 计数器用于取消：停止/切换模式会 bump，旧任务看到 generation 变化就退出；
- `FishTTS` 在后台保持一条 `start` 过的备用连接，语音提交时直接 `text/flush/stop`，把 Fish 握手从首段延迟里拿掉。

## 模式切换

运行中切换时：

1. 校验新模式（key、音色、虚拟麦克风）；
2. bump work generation，取消当前 worker（正在合成的 Fish 连接关闭）；
3. 清空 `SegmentQueue`；
4. 清空虚拟麦克风和监听缓冲；
5. 按新模式重建提交规则和 Fish 连接（日语/中文音色不同，备用连接也必须换）；
6. 启动新 worker，继续用同一个 ASR/麦克风会话。

因此新音频不会和旧模式音频混在一起。

## Keychain

```text
chc-voice.dashscope  → DASHSCOPE_API_KEY
chc-voice.fish       → FISH_AUDIO_API_KEY
chc-voice.llm        → CHAT_LLM_API_KEY
```

`settings_store.Keychain` 只通过 `/usr/bin/security` 读写；读取失败返回 None；写入失败只报退出码，不把参数（含 key）放进日志。开发环境可用 `.env` 作为回退，但 UI 状态会标注 `source: env`。

## 指标

`~/Library/Application Support/chc-voice/metrics.jsonl`：

- `startup` / `shutdown`
- `segment`：`translation_ms`、`tts_first_audio_ms`、`total_ms`、字数
- `translation`：耗时、字数
- `error`：服务名、消息、HTTP 状态

不记录识别文本、翻译文本、key 或音频。

## 核心设计取舍

- **准确优先**：默认不在无标点停顿处切句；用户明确接受为准确率增加延迟。
- **不依赖旧项目**：Fish/Bailian 的协议实现是本项目自己的代码，旧项目只用于参考协议；运行时不 import 旧项目。
- **虚拟麦克风不写驱动**：用 BlackHole + Aggregate Device，MVP 不写 AudioServerPlugIn。
- **聚合设备用 CoreAudio API 创建**：不要求用户手动点 Audio MIDI Setup，也不需要管理员权限。
