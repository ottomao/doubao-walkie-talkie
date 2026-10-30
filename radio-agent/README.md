# 对讲机语音助手核心

无界面服务，使用一块 AIOC 在 Mac 上连接对讲机，通过火山引擎豆包端到端实时语音模型完成轮次对话。接收、云端对话、发射和日志各自独立，后续桌面应用可调用 `Engine`，也可启动 `radio-agent` 子进程读取 JSONL 输出。

当前状态：真实云端往返和无线收发已完成，用户确认回复清晰完整。发射时 USB 偶发掉线仍未解决，连续多轮稳定性和延迟尚待验证。

## 安装与检查

以下命令在仓库根目录执行：

```sh
python3 -m venv radio-agent/.venv
radio-agent/.venv/bin/pip install -r radio-agent/requirements.lock -e ./cli -e ./radio-agent
cp radio-agent/.env.example radio-agent/.env
chmod 600 radio-agent/.env
source radio-agent/.venv/bin/activate
```

填写凭证后，运行 `radio-agent doctor` 检查唯一 AIOC、PTT 配置和凭证是否存在，不向云端发请求。`radio-agent listen --seconds 10` 只验证收音和分段，不上传、不发射。

## 火山服务和凭证

开通豆包端到端实时语音大模型 **O2.0**，模型参数 `1.2.1.1`。使用普通音色 `zh_male_yunzhou_jupiter_bigtts`，无需声音克隆、联网搜索或 RTC 服务。本版使用具有 `push_to_talk` / `EndASR` 的二进制 Realtime API；不是 Seeduplex 3.0 的全双工 JSON API，二者配置和事件不能混用。

编辑本地 `radio-agent/.env`，二选一：

- 新语音控制台 API Key：填写 `VOLC_API_KEY`（优先采用）。
- 旧语音控制台应用：填写 `VOLC_APP_ID` 和 `VOLC_ACCESS_KEY`（Access Token，不是通用云 AK/SK）。

本机已确认新版 API Key 可调用本版接口。若其他账号仅开通 Seeduplex 3.0，应调整服务授权或另行增加该协议适配，不应只替换 URL。

配置不会被提交到 Git；日志不输出凭证。环境变量优先于 `.env`。可通过全局参数 `--env-file /absolute/path/to/.env` 选择其他配置，日志相对路径以配置文件目录为基准。

先准备自己的单声道 PCM16 WAV 文件（下面使用 `recordings/input.wav`），验证一轮云端往返，默认不会发射：

```sh
radio-agent replay recordings/input.wav
```

检查生成的 `transcript.txt`、`reply.wav` 和 `tx.wav`。需要把该轮回复发射出去时：

```sh
radio-agent replay recordings/input.wav --transmit
```

接着验证真实语音触发，再进入持续运行：

```sh
radio-agent run --once
radio-agent run
```

终端出现 `"event": "listening"` 后，可以用另一台同频道对讲机说话。每次说完松开 PTT，等待 AI 回复完再开始下一轮。`Ctrl+C` 或 SIGTERM 停止；发射子进程会收到终止信号并释放 PTT。

可用 `radio-agent run --seconds 60` 限时运行。发送 `kill -HUP <进程PID>` 会请求在当前轮次结束后清空上下文；重新启动也会新建会话。

## 工作流程

1. 校验 AIOC 已启用串口 PTT、关闭 AutoPTT，且只有一块板卡。
2. 连接云端，在本地以 48 kHz、单声道 PCM16 持续采集，不使用 Mac 默认麦克风。
3. 用 WebRTC VAD 和能量门限过滤静音与短脉冲；确认人声后携带最多 300 ms 前置音频。
4. 有状态重采样到 16 kHz，按实时节奏发送 20 ms / 640 字节的音频包。
5. 连续静音约 1 秒后发送 `EndASR`，让本地负责轮次边界，不与服务端 VAD 重复触发。
6. 收集用户转写、AI 文本、24 kHz PCM16 回复，等待识别、文本、音频结束事件。
7. 将回复重采样到 48 kHz，应用已验证的 0.75 幅度，调用独立 AIOC 子进程连续发射。
8. 释放 PTT，等待 400 ms 尾音保护，再恢复监听。

第一版一次处理一轮：等待回复和发射期间不接纳新的输入，输入队列会清空，避免把旧音频或回送尾音当成新问题。该时段也不做录音/转写，不承诺记录抢话内容。单台半双工对讲机在发射时本身无法接收远端抢话。

## 参数与异常处理

参数见 `.env.example`，默认值：

| 参数 | 默认 | 含义 |
|---|---:|---|
| `RADIO_TX_GAIN` | 0.75 | 发射幅度 |
| `RADIO_SILENCE_SECONDS` | 1.0 | 判定说话结束的静音长度 |
| `RADIO_VAD_MIN_DBFS` | -42 | 有效人声的最低能量门限 |
| `RADIO_MAX_INPUT_SECONDS` | 30 | 每轮输入上限 |
| `RADIO_MAX_REPLY_SECONDS` | 20 | 每次回复音频上限 |
| `RADIO_REPLY_TIMEOUT_SECONDS` | 45 | 提交输入后等待完整回复的上限 |
| `RADIO_IDLE_RESET_SECONDS` | 300 | 空闲后清空会话 |
| `RADIO_GUARD_SECONDS` | 0.4 | 发射结束后保护间隔 |

- 输入超过上限：保存已接收部分并记录失败，不发射截断问题的回复；等待频道安静再接收下一轮。
- 回复过长：优先截到上限以内最近的完整 TTS 句子；如果第一句已经过长则硬截并淡出。保留完整生成文本和音频，明确标记截断，并重置上下文，避免模型误以为听众听到了完整回复。
- 回复超时、网络断开或输入溢出：记录失败、丢弃待播回复，重连后不重发旧内容。连接采用最多 5 次退避重试；鉴权错误直接停止。不可恢复的设备错误退出，便于上层桌面应用明确显示异常。
- 发射采用独立子进程和时限。正常退出、Ctrl+C、SIGTERM 会清理；超时会先终止，必要时强制结束并另行尝试释放串口。SIGKILL/整机故障无法保证清理，持续发射时断开 AIOC。
- 同一用户只允许一个 `radio-agent` 实例。不要同时运行其他对讲机控制软件。
- 文字缺失或只有中间结果会在日志中标记 `missing` / `partial`，音频仍保留，不编造转写。最终识别质量需实际通话验证。

## 记录结构

默认目录 `radio-agent/data/` 已被 Git 忽略。每次启动有一个运行目录，每轮对话有独立子目录：

```text
data/20261025-203500-xxxxxxxx/
  events.jsonl
  0001-xxxxxxxx/
    turn.json
    transcript.txt
    rx.wav
    uplink.wav
    reply.wav
    tx.wav
```

- `events.jsonl`：UTC 时间、相对耗时、运行/轮次/会话 ID、云端文本事件及请求日志 ID、状态变化、错误。音频事件只记长度，音频本体存 WAV。
- `turn.json`：最终文字、转写是否最终、生成语音文本、文件映射、耗时、完成/失败/中断、发射/未发射/截断状态。
- `rx.wav`：AIOC 原始 48 kHz 收音。
- `uplink.wav`：实际送给云端的 16 kHz 音频。
- `reply.wav`：云端返回的原始 24 kHz 音频，包括未播出的内容。
- `tx.wav`：应用增益、重采样、必要截断后的 48 kHz 发射目标音频。它是送给声卡的数据，不是对方电台的接收录音；是否成功收听仍需远端确认。

日志逐事件写入，音频增量写入可回放 WAV；文字更新用原子替换。失败和中断也保留现场。默认不保存全天静音、不自动清理历史文件；需按磁盘容量自行归档。`captured_only` 表示本地监听检查，没有调用云端，因此没有文字识别结果。

## 测试

覆盖协议帧、语音分段、重采样、文本修订、事件乱序、日志脱敏、本地 WebSocket 往返、异常清理和上下文重置。测试不会调用真实云端或发射。

```sh
radio-agent/.venv/bin/python -m unittest discover -s radio-agent/tests -v
```

## 实现目录

`settings.py` 管理配置；`audio.py` 负责采集/VAD/重采样；`protocol.py` 编解码协议；`cloud.py` 管理 WebSocket/回复归集；`engine.py` 组织轮次与发射；`journal.py` 保存证据；`cli.py` 负责命令、进程锁和信号。后续更换模型供应商时优先替换云端适配层。

协议依据：[官方 Realtime API](https://docs.volcengine.com/docs/DoubaoVoice/End-to-endreal-timespeechlargemodelAPIaccessdocument?lang=zh)、[API Key 使用说明](https://docs.volcengine.com/docs/DoubaoVoice/APIKeyUsage)、[旧版凭证说明](https://docs.volcengine.com/docs/DoubaoVoice/old-version-console-authentication-reference-example?lang=zh)。
## USB 拔插与停止

运行期间检测到 AIOC 断开或重新插入时，停止本次运行；重新插好设备后重新启动，不复用旧音频连接。发射失败会额外尝试释放 PTT；设备已断开时软件无法保证释放成功。若仍占用信道，先关闭对讲机电源或拔掉对讲机侧连接线。

启动：在仓库根目录执行 `radio-agent/.venv/bin/radio-agent run`。停止：`Ctrl+C`。手动释放：`cli/.venv/bin/aioc --json release`。

拔插防护的单元测试已覆盖串口路径复用、释放失败时关闭串口、发射子进程失败后重试释放；实际 macOS 热拔插和音频路由仍需实机验收。
