# AIOC 命令行工具

面向 macOS 的 AIOC 串口 PTT 和 USB 音频工具。控制层位于 `src/aioc/device.py`，命令入口位于 `src/aioc/cli.py`。后续桌面应用可导入控制层，或启动 CLI 子进程读取 JSON。

## 安装

在仓库根目录运行：

```sh
python3 -m venv cli/.venv
cli/.venv/bin/pip install -e ./cli
source cli/.venv/bin/activate
aioc --help
```

不激活环境时，用 `cli/.venv/bin/aioc` 替代 `aioc`。

## 使用

```sh
aioc devices
aioc doctor
aioc config
aioc audio-check
aioc record /tmp/radio-rx.wav --seconds 5
aioc listen --seconds 10
aioc ptt --seconds 1
aioc release
aioc transmit /tmp/radio-rx.wav --gain 0.75
aioc --json devices
```

- `doctor` 打开串口并将 DTR/RTS 置低，检查音频格式支持；不会主动置高 PTT。
- `record` 只从 AIOC 录音，生成单声道、16-bit PCM、48 kHz WAV。目标文件必须不存在。`rms_dbfs` 为录音电平，完全静音时为 `null`。
- `listen` 将 AIOC 输入送到系统默认输出；请将 Mac 扬声器或耳机设为默认输出。默认输出为 AIOC 时拒绝运行，避免回送。
- `ptt` 让连接的对讲机发射指定秒数；`transmit` 发射指定 WAV 文件，默认幅度缩小至 25%。请在已配置好的可用频道上运行。
- 本机实际验收使用 `--gain 0.75`，用户确认音量与清晰度正常；以上示例采用该值。
- 所有时长必须大于 0 且不超过 60 秒。`transmit` 另有 150 ms 起发等待和 100 ms 尾音等待。
- `release` 清除串口 DTR/RTS；不清除其他软件通过 HID 或设备 AutoPTT 设置产生的发射。
- `--port /dev/cu.usbmodem…` 适用于 `doctor`、`ptt`、`release`、`transmit`；`--input INDEX` 适用于录音与监听；`--output INDEX` 适用于发射。索引由 `devices` 查询，重新插拔后可能变化。
- 自动选择要求对应接口唯一，不会回退到 Mac 麦克风或其他串口。音频与串口手动选择时应由调用方确保来自同一块板。
- `--json` 放在子命令之前。成功 JSON 输出到 stdout，失败 JSON 输出到 stderr；退出码为 0、1，或中断时的 130。

macOS 首次录音可能要求终端/宿主应用的麦克风权限；如果不能录音，在系统设置的“隐私与安全性 → 麦克风”中允许运行 CLI 的应用。

## 控制协议与边界

`aioc config --serial-ptt` 关闭 AutoPTT、启用串口 PTT，保留其他触发源；仅修改 RAM，不写 Flash。重新上电后可能需要重新执行。多个板卡时使用 `--serial SERIAL`。

`aioc audio-check` 不触发 PTT，播放两秒静音并测量耗时。测试设备升级至官方 v1.4.1 后，播放耗时从 4.434 秒恢复为 2.165 秒，用户确认实际语音清晰完整。详见 [固件说明](../hardware/firmware/README.md)。

按 [AIOC 官方使用说明](https://github.com/skuep/AIOC/wiki/Getting-Started)，USB 标识为 `1209:7388`，PTT 为 `DTR=1, RTS=0`。串口打开前先设置 DTR/RTS 为低，结束、异常、Ctrl+C、SIGTERM 时释放。SIGKILL、进程崩溃或系统故障无法执行软件清理；出现持续发射时可运行 `release`，必要时断开 AIOC。

AIOC 提供音频与串口通道，不等于对讲机的通用调频 API。本阶段不含频道、频率、功率读写；这些需要确认对讲机型号与编程协议。USB 的 Version 1.00 不能直接认定为固件版本。

## 验证与限制

已验证双向语音、音量和清晰度。但连续测试中出现发射时 USB 掉线和 PTT 卡住，尚未解决，详见 [项目说明](../README.md#已知问题)。不要把单次发射成功视为长期稳定性保证。

## 测试

```sh
cli/.venv/bin/python -m unittest discover -s cli/tests -v
```

覆盖中断/音频故障时 PTT 释放、时长约束、多设备选择，以及禁止回退到系统麦克风。测试通过 mock 执行，不会发射。
