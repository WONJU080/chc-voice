# 延迟

## 实测（本机，2026-10-29）

| 场景 | 首段音频 | 备注 |
|---|---:|---|
| 模式 A，Fish 已预热 | ≈ 0.5–1.0 s | 从 commit 到写入虚拟麦克风；预热连接已完成时接近 0.5 s |
| 模式 A，冷连接 | ≈ 1.1 s | 每次新建 Fish WebSocket |
| 模式 B，翻译 + 预热 | ≈ 1.1 s | 翻译 ≈ 590 ms，Fish ≈ 500 ms |
| 停止 → 静音 | < 100 ms | 清空缓冲后回调立即写零 |

这些数字是实测，不是承诺。Fish 的服务器、代理、语音长度、网络抖动都会改变结果。

## 时间花在哪里

### 模式 A

```text
麦克风帧 → ASR partial（约 0.3–0.9 s 首字）
        → 稳定前缀等待 stable_ms（250 ms）
        → 标点/final 提交
        → Fish 首块音频（预热 ≈ 0.4 s，冷连接 ≈ 1.0 s）
        → 写入 BlackHole（下一个 10 ms 回调）
```

### 模式 B

```text
模式 A 的 ASR/提交
  + 翻译 LLM（约 0.3–0.9 s）
  + Fish 首块音频（预热 ≈ 0.4 s）
```

用户已明确接受“为准确率增加延迟”，所以默认：

- `pause_flush_ms = 0`：不在无标点停顿处切句；
- 只在标点、ASR final、或超过 `max_chars` 时提交；
- 这样“我明天要去东京开会”不会被切成“我明天要去东京开”+“会”，翻译不会出现半截语义。

## 队列语义

各段 TTS 是顺序播放的。如果用户连续说话，后面提交的段会排在前一段音频之后：

- 第一段的首段延迟是上面表格里的值；
- 后续段的 `tts_first_audio_ms` 会包含等待前一段播放的时间，不代表 Fish 变慢；
- 正常语速下 TTS 播放速度通常快于说话速度，队列不会无限增长；`SegmentQueue` 满 32 段会丢弃并提示。

## 可调参数

`~/Library/Application Support/chc-voice/relay.toml`：

```toml
[commit.zh_direct]
stable_ms = 250
min_chars = 6
max_chars = 40
pause_flush_ms = 0

[commit.zh_to_ja]
stable_ms = 350
min_chars = 10
max_chars = 60
pause_flush_ms = 0
```

- 想更早出声：把 `pause_flush_ms` 调到 300–600，或降低 `stable_ms` / `min_chars`。代价是可能切在词中间。
- 想更准：增大 `stable_ms`、`min_chars`，或让 `pause_flush_ms=0`。
- 修改后需要重新“开始”才生效（提交规则在开始时构建）。

## 进一步优化（未做）

- 用更近的 Fish 区域/节点，或降低代理 RTT；
- 使用 Fish 的 chunk 参数（`chunk_length` / `min_chunk_length`）换取更早的首块；
- 流式翻译（边译边合成）——当前是整句翻译后再合成，准确率优先；
- 句级 VAD 与 ASR endpoint 调参；
- 用本地模型替代部分云端调用。
