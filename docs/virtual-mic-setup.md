# 虚拟麦克风设置

macOS 的普通应用不能直接“变成麦克风”。其他软件只从 Core Audio 输入设备读取音频，所以这里用：

```text
BlackHole 2ch（真正收音频的驱动）
  + Aggregate Device 名称：chc-voice
  + 后端把 48 kHz 双声道 PCM 写进 BlackHole 2ch
  + 微信/游戏/Zoom/Discord 选择 chc-voice
```

## 1. 安装 BlackHole 2ch

```bash
brew install --cask blackhole-2ch
```

或者：

```bash
scripts/install_blackhole.sh
```

脚本下载 `BlackHole2ch-0.7.1.pkg`、校验 SHA-256、调用系统 installer，并要求管理员授权。它不会静默安装。

安装后刷新 Core Audio：

```bash
sudo killall coreaudiod
```

如果设备仍然不出现，注销并重新登录（BlackHole 安装器本身也建议重启）。

检查：

```bash
.venv/bin/chc-voice devices
```

应看到：

```text
BlackHole 2ch  in=2 out=2
```

## 2. 创建 Aggregate Device

```bash
.venv/bin/chc-voice aggregate create
```

它会通过 Core Audio 创建一个：

```text
名称：chc-voice
子设备：BlackHole 2ch
输入：2 声道
输出：2 声道
UID：chc-voice.aggregate.BlackHole2ch_UID
```

创建 Aggregate Device **不需要管理员权限**，也不会改系统默认输入。

检查：

```bash
.venv/bin/chc-voice aggregate status
```

UI 主面板的“虚拟麦克风：chc-voice ✓”变绿即可。如果显示 ✗，设置抽屉里会出现“创建虚拟麦克风”按钮。

## 3. P0-A 回环验证

```bash
.venv/bin/python scripts/p0_loopback.py
```

脚本会：

1. 确认 `chc-voice` 存在；
2. 向 BlackHole 2ch 写入 1.2 秒 440 Hz；
3. 从 `chc-voice` 录音 2.5 秒；
4. 计算 RMS/峰值，保存 `artifacts/p0-loopback.wav`；
5. RMS 低于阈值就失败。

本机结果：

```text
录音：126720 帧，RMS=3882.6，峰值=8191
P0-A 通过
```

如果录到全 0：

- 系统设置 → 隐私与安全性 → 麦克风，允许终端 / DeepSeek Harness；
- 确认没有把“输入设备”设成 `chc-voice` 自己（会自激）；
- 确认没有另一个程序独占 BlackHole。

## 4. 让其他软件使用

### 微信

设置 → 音频 → 麦克风 → `chc-voice`

### 游戏

音频设置 → 麦克风 → `chc-voice`

### Zoom / Discord

麦克风选择 `chc-voice`

### 使用系统默认输入的程序

系统设置 → 声音 → 输入 → `chc-voice`

### 不显示 Aggregate Device 的软件

回退选择 `BlackHole 2ch`。它和 `chc-voice` 收到的是同一路音频，只是名字不同。UI 会说明这一点。

## 5. 测试与电平

- UI “测试虚拟麦克风”写入 1.2 秒 880 Hz；
- 主面板两条细电平条：上为虚拟麦克风输出，下为真实麦克风输入；
- 设置里的“总音量”只影响写入虚拟麦克风的音量；“监听音量”只影响耳机监听，两者独立；
- 限幅器把峰值压在 1.0 以下，避免削波。

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| `chc-voice` 不在输入设备列表 | `aggregate create`；仍不行重启 Core Audio |
| 微信能选但对方听不到 | 确认已点“开始”，模式可用，虚拟麦克风状态 ✓ |
| 对方听到断续/杂音 | 检查 underrun；关闭其他占用 BlackHole 的程序；确认采样率 48k |
| 监听啸叫 | 戴耳机；关闭监听；紧急停止 |
| 系统升级后设备消失 | 重新安装 BlackHole，重建 Aggregate Device |

不要让“输入麦克风”和“虚拟麦克风”指向同一个设备，否则会形成自激环路。
