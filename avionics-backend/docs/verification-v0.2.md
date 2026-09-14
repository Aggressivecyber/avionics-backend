# v0.2 验证记录

日期：2026-09-15。编译平台：Windows x64 / MSYS2 MinGW-w64 g++ 15.2.0，CMake 3.28.1，Ninja 1.11.1，C++20，Debug。没有在 Linux 编译或连接真实总线。

## 编译与 C++ 测试

命令：`./scripts/build-mingw.ps1 -RunTests -RunDemo`。最终退出码 0，28 个构建步骤完成，无编译 warning/error。CTest 的 2 个集成测试程序全部通过，总用时 1.50 秒。

- 原插件测试：双实例、连续 20 次替换、ABI/配置拒绝、启动失败恢复、停止回调退出、库句柄生命周期、队列满、存储失败、CRC/截断、跨插件切换及逐帧回放一致性。
- 新处理测试：严格配置、大小端跨字节字段、符号扩展、64 位整数、float64、布尔、枚举、缩放、无效值、时钟映射、重复/乱序、新旧代次、截止边界、无效样本不推进水位、观测缺口、EOF 未完成、时间误差界、单位/时间组隔离、已知延迟和 JSONL。

日志位于 `build-gcc-debug/build.log` 和 `test.log`。测试输出分别位于：

- `build-gcc-debug/test-output/160603575037000/`
- `build-gcc-debug/processing-output/160604730048000/`

日志里的 V: 为脚本临时映射，运行结束已经解除。

## 确定性关联演示

目录：`runs/correlation_639250094945893027/analysis/`，输入为同级 `input.avbus`。

```text
records=17 samples=17 usable=17 rejected=0 results=5
consistent=1
inconsistent=1
response_observed=1
response_timeout=1
response_unresolved=1
```

已知指令位于 10 ms、响应位于 22 ms，输出延迟为 12 ms，证据引用原记录 5 和 7。另有一个窗口内未观察到响应的事件，以及一个记录结束时仍未完成的窗口。样例不是飞机实际参数或故障证据。

## 在线与离线等价检查

命令：`python scripts/verify_live_roundtrip.py`，退出码 0。该脚本仅用 Python 标准库作独立验收，后端和 CMake/CTest 不依赖 Python。

目录：`runs/live_roundtrip_1789412894246217200/`。

```text
live_exit=0 offline_exit=0
samples=48 results=19 rejected=5
byte_identical_parameters=true
byte_identical_events=true
changed_source_generations=2
unaffected_source_generations=1
```

检查通过公开 CLI 启动两个模拟源，在运行中替换 sensor_a，再用原始归档离线分析。在线和离线的 parameters.jsonl、events.jsonl 逐字节相同，来源与代次断点正确。

本次 5 条样本被标记为到达时序不满足组水位，未进入关联，但仍保存在原始记录和参数 JSONL。这里的 rejected 是分析质量拒绝，不是采集队列丢失。样本数和到达乱序量受线程调度影响，每次实时验收可能不同；确定性离线样例的结果应保持不变。

另使用独立 JSON 解析器核对 C++ 导出的 18446744073709551615 整数、引号和换行转义、中文枚举标签，以及 NaN 的 null/无效标记，全部通过。

## 证据边界

这次验证没有证明实际总线吞吐、长时间稳定性、硬件同步、物理热插拔或飞行故障识别能力。clock group 和 offset 需要外部已验证依据；当前没有迟到重排、插值、硬件自动同步、真实 ICD 映射或平台网络服务。

初版源码已在扩展前保存于 `snapshots/`。当前源码、编译产物、日志及关键输出的 SHA-256 见 `verification-v0.2.json`；该快照只对应本次验证，后续编译会改变二进制和日志。
