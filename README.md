# 豆包对讲机语音助手

通过 AIOC 开发板，把普通对讲机连接到 Mac 和火山引擎语音大模型。在另一台对讲机上按住发射键说话，松开后等待 AI 通过无线频道回复。

项目提供独立硬件命令行工具和无界面语音服务，方便后续接入桌面应用。

## 当前状态

- 已实现 AIOC 收音、音频发射、PTT 控制和设备检查。
- 已接通豆包端到端实时语音模型，支持新版 API Key。
- 已完成真实语音往返测试，接收端确认回复清晰、完整。
- 支持连续多轮上下文，保存双方文字、收发录音和运行日志。
- **仍处于实验阶段：测试中出现发射时 USB 掉线、PTT 持续占用的现象，连续运行稳定性尚未解决。** 射频干扰是排查方向，尚未确认根因。
- 主要在 macOS 上验证，未完成 Windows、Linux 适配验证。

## 工作方式

```text
手持对讲机 → 无线频道 → 对讲机 + AIOC → Mac → 火山语音模型
手持对讲机 ← 无线频道 ← 对讲机 + AIOC ← Mac ← 模型回复
```

本地检测人声并流式上传，连续静音约 1 秒后结束本轮。收到完整回复后才发射，响应速度受网络、模型和回复长度影响。

这是半双工应用：等待回复和发射期间不接纳、录制新的问题。频道按人与 AI 对话设计，不区分说话人，不需要唤醒词。同一次运行保留上下文，默认空闲 5 分钟后重置；重启不会自动加载历史对话。

## 准备条件

- Mac 和 Python 3.11 或更高版本。
- AIOC、支持数据传输的 USB 线、接口兼容的对讲机。
- 另一台配置为相同可用频道的对讲机，用于通话测试。
- 火山引擎语音服务凭证，并开通对应的实时语音服务。
- 允许终端应用使用麦克风。

项目控制音频和发射键，不提供频道、频率、功率读写；请先在对讲机上完成配置。

## 快速开始

### 安装

```sh
git clone https://github.com/ottomao/doubao-walkie-talkie.git
cd doubao-walkie-talkie
python3 -m venv radio-agent/.venv
radio-agent/.venv/bin/pip install -r radio-agent/requirements.lock -e ./cli -e ./radio-agent
```

两个模块安装在同一虚拟环境中。后续命令均在仓库根目录执行。

### 配置密钥

```sh
cp radio-agent/.env.example radio-agent/.env
chmod 600 radio-agent/.env
```

编辑 `radio-agent/.env`，填写自己的 `VOLC_API_KEY`。新版语音控制台 API Key 已验证可单独鉴权，不需要 App ID。真实密钥不要写入示例文件或源代码；环境变量优先于配置文件。

当前实现使用豆包端到端实时语音模型 O2.0，模型参数 `1.2.1.1`，采用二进制实时语音接口，不能直接替换为 Seeduplex 3.0 的全双工 JSON 接口。旧版鉴权和完整参数见 [语音助手说明](radio-agent/README.md)。

### 检查与运行

```sh
radio-agent/.venv/bin/aioc devices
radio-agent/.venv/bin/aioc config
radio-agent/.venv/bin/radio-agent doctor

# 仅验证本地收音和语音分段，不访问模型、不发射
radio-agent/.venv/bin/radio-agent listen --seconds 10

# 持续运行
radio-agent/.venv/bin/radio-agent run
```

只连接一块 AIOC，并启用串口 PTT、关闭 AutoPTT。不符合要求时，可运行 `radio-agent/.venv/bin/aioc config --serial-ptt` 临时调整；不写入 Flash，重新上电可能需要再次执行。

看到 `listening` 事件后，在另一台对讲机上说话，松开发射键后等待回复。回复结束后再说下一句。按 `Ctrl+C` 停止；`run --once` 只运行一轮，`run --seconds 60` 限时运行。终端输出采用 JSONL，便于其他程序读取。

## 日志与密钥保护

默认在 `radio-agent/data/` 按运行和轮次保存双方文字、状态、耗时，以及原始收音、上传音频、模型回复和准备发射的音频。检测到的语音会发送到火山引擎；本地日志和录音可能包含私人信息，不随仓库发布，也不会自动清理。

`.gitignore` 排除了 `.env`、虚拟环境、录音、默认日志目录和固件二进制。只提交空白凭证模板 `.env.example`。修改日志目录时也要更新忽略规则。提交前检查暂存区，不要强行加入这些文件。密钥如曾公开，应立即更换，删除文件不能撤销泄露。

## 已知问题

### 发射断续

测试设备升级到官方 AIOC v1.4.1 后，macOS 播放时序和实际音质恢复正常。详见 [固件说明](hardware/firmware/README.md)。默认增益为 `0.75`，具体设备仍需实测调整。

### USB 掉线与持续发射

已观察到发射开始后，串口和音频接口一起消失。设备离线时软件无法保证释放 PTT。若持续发射，先关闭连接 AIOC 的对讲机电源，或断开对讲机侧连接。恢复连接后重新启动程序。设备在线时可尝试：

```sh
radio-agent/.venv/bin/aioc --json release
```

先使用低功率，确认插头牢固，让 USB 线远离天线，再逐项检查线材和抗干扰措施。[AIOC 官方排障说明](https://github.com/skuep/AIOC/wiki/Troubleshooting) 记录了射频干扰导致掉线的类似现象，但本项目的根因尚未确认。

程序包含连接变化检测、发射超时和失败后的释放尝试，不保证热插拔自动恢复，也无法兜底硬件断连、进程强制终止或系统故障。

## 项目结构与测试

| 目录 | 用途 |
|---|---|
| [cli/](cli/README.md) | 独立 AIOC 控制包和命令行工具 |
| [radio-agent/](radio-agent/README.md) | 语音检测、模型连接、会话调度和日志 |
| [hardware/firmware/](hardware/firmware/README.md) | 固件诊断说明，不分发固件文件 |

```sh
radio-agent/.venv/bin/python -m unittest discover -s cli/tests -v
radio-agent/.venv/bin/python -m unittest discover -s radio-agent/tests -v
```

测试使用模拟硬件和本地 WebSocket，不会实际发射，也不需要云端密钥。硬件稳定性和实际收听效果仍需实机验收。

## 开源协议

本仓库自有代码采用 [MIT 许可证](LICENSE)。AIOC 硬件、固件和第三方依赖遵循各自的许可证；火山引擎服务遵循其服务条款。
