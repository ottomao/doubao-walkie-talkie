# AIOC 固件说明

本目录仅保留诊断说明，不分发第三方固件或设备 Flash 备份。固件及硬件遵循 AIOC 上游许可证，不属于本仓库 MIT 授权范围。

## 已验证的升级结果

测试设备原先在 macOS 上发射语音断续。两秒静音播放实际耗时 4.434 秒，切换 PTT 控制方式未改善。升级到官方 v1.4.1 后，耗时恢复到 2.165 秒；实际对讲机通话音量和清晰度正常。

升级后 PTT1 为 `0x0404`，支持串口 DTR 与 CM108 GPIO3，AutoPTT 关闭。USB 描述中的版本号不能直接当作固件版本。

这次升级解决了音频时序问题，但未解决后续发现的发射时 USB 掉线、PTT 卡住问题，详见 [项目说明](../../README.md#已知问题)。

## 获取固件

- [官方 v1.4.1 发布页](https://github.com/skuep/AIOC/releases/tag/v1.4.1)
- [v1.4.0 的 macOS 音频修复说明](https://github.com/skuep/AIOC/releases/tag/v1.4.0)
- [官方排障说明](https://github.com/skuep/AIOC/wiki/Troubleshooting)

请按上游文档确认板卡兼容性和 DFU 操作方法，升级前备份自己的设备。不要照抄其他设备的序列号或刷写参数。本地下载的 `.bin` 和设备备份已加入 Git 忽略规则。
