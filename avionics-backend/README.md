# 地面总线监测后端 · v0.3

面向地面试验、总线监测和数据分析的 C++20 软件基线。当前可运行模拟采集、回放、配置化参数解码、事件时间处理和关联分析。真实总线协议、采集卡 SDK、ICD、时钟同步与设备负载验收尚待选型。

项目尚未上线，按用户要求仅保留插件 ABI 2、归档 2、配置 schema 2、结果 schema 2。旧插件统一重做，旧试验输入重新生成。当前版本、源码提交和格式版本可通过 `--version` 查询。

[当前架构与图示](docs/architecture.md) · [升级验收计划](docs/upgrade-plan.md) · [验证记录](docs/upgrade-verification.md) · [设备与成品验收输入](docs/product-acceptance.md)

## 本轮已落地

- 每个采集来源一个工作进程，插件加载、配置和启停均受超时监督；SDK 用户态故障与宿主分离。
- 原始记录和分析各有线程、条数及字节预算；分析异常或积压时原始记录继续，游标和缺口写入会话结果。
- 按来源限制入口条数、字节和整个会话的来源状态容量，拒绝原因及计数可追溯。
- 配置加载前检查参数状态预算；字段、时钟、输入规则和活动响应截止均使用索引。
- 显式迟到窗口和有界事件重排，水位截止、未知时钟、序号质量和重启边界有回归。
- 原始记录具有逐记录 CRC32、会话身份、记录数/长度/SHA-256 封存；配置快照来自实际解析字节。
- 参数及关联证据保留会话、配置摘要和原始记录位置；在线与离线共用处理实现。
- 异步 SDK 错误、启动代次、来源配置和替换事件写入生命周期日志；移除来源不会清掉会话失败状态。

## Windows 构建与验证

在 E 或 D 盘工作副本内执行，构建和运行输出保留在该盘。当前已实测 Windows 11 x64 / g++ 15.2 / CMake 3.28.1 / Ninja 1.11.1。

```powershell
$env:PATH = 'C:\msys64\mingw64\bin;' + $env:PATH
cmake -S . -B build-gcc-release -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=C:/msys64/mingw64/bin/gcc.exe -DCMAKE_CXX_COMPILER=C:/msys64/mingw64/bin/g++.exe
cmake --build build-gcc-release --parallel 2
ctest --test-dir build-gcc-release --output-on-failure
python scripts/verify_live_roundtrip.py --build build-gcc-release --config config/ground-monitor.example.ini
python scripts/verify_analysis_recovery.py --build build-gcc-release
python scripts/verify_capture_cli.py --build build-gcc-release
```

若 CMake/Ninja 不在 PATH，使用其已安装绝对路径。中文工作路径可使用既有 `scripts/build-mingw.ps1`；它临时映射 ASCII 盘符，数据仍写在工作副本所在磁盘。

## 启动与结果

```powershell
.\build-gcc-release\bin\bus_backend.exe --version
.\build-gcc-release\bin\bus_backend.exe --interactive E:/bus-runs/session.avbus config/ground-monitor.example.ini
```

归档父目录须已存在，文件与分析输出目录须是新路径。`bus_capture_worker.exe` 放在宿主旁边，运行中始终由宿主管理；没有单独启动采集工作进程的操作步骤。

控制台支持 `add/start/stop/remove/replace/status/stats/events/quit`。例如 `add sensor_a E:/path/bin/bus_simulated.dll period_ms=5`，然后 `start sensor_a`。开发控制台的插件路径暂不支持空格；设备 API 和图形界面尚未实现。

正常退出写入 `session.avbus`、`session.avbus.session.json`、`session.avbus.lifecycle.jsonl` 和 `session.avbus.analysis/`。分析文件包括参数、事件和准确配置副本。会话结果独立报告采集、记录和分析状态，不能仅看一个文件是否生成。

退出码：0 为软件会话成功；1 为原始存储或致命错误；2 为命令行用法错误；3 为分析未完成但原始数据可能仍完整；4 为来源故障或入口丢弃使采集降级。SDK 故障后的分析用于诊断，降级会话不能作为成功采集验收。

## 离线校验与重算

```powershell
.\build-gcc-release\bin\bus_analyze.exe --fixture E:/bus-runs/example.avbus
.\build-gcc-release\bin\bus_analyze.exe --inspect E:/bus-runs/example.avbus
.\build-gcc-release\bin\bus_analyze.exe --analyze config/correlation-demo.ini E:/bus-runs/example.avbus E:/bus-runs/example-analysis
```

`--inspect` 不需要参数字典。`--analyze` 的 `status=complete` 表示本次分析处理完成；输入封存状态和原采集会话状态分别判读。原始归档只有成功验证封存后才可作为完整输入；异常中断文件会明确失败，原文件保留以供后续恢复取证。

## 设备插件与发布

[插件契约](docs/plugin-contract.md)、[采集隔离](docs/capture-isolation.md)、[事件时间](docs/event-time.md)、[归档格式](docs/archive-v2.md)。协议扩展接口仍处于契约阶段，自动识别器与实际协议解析器尚未实现，见 [协议抽象](docs/protocol-abstractions.md)。

源码提交后重新配置并构建，确保 `--version` 无 `dirty` 标记，再运行：

```powershell
python scripts/package_release.py --build E:/path/build-gcc-release --output E:/avionics-releases/v0.3.0
```

便携包包含宿主、采集进程、分析程序、两个内置插件、匹配的 MinGW 运行库及许可证、配置、文档、源码导出和 SHA-256 清单。发布使用独立目录，退出旧会话后切换目录；原始归档和旧目录保留，便于回退开发改动。失败替换会尝试恢复旧实例并明确报告结果。

Linux 路径提供 OpenSSL Crypto、POSIX 进程组和父进程退出处理实现，需安装 OpenSSL 开发包。Windows 与 Ubuntu CI 配置已提供，远端 CI 与 Linux 原生运行未在本地验收。Linux 构建命令为常规 `cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release`、`cmake --build build`、`ctest --test-dir build --output-on-failure`。
