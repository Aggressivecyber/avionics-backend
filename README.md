# AEROBUS · 地面总线试验与分析工作台

当前版本 v0.5.0，集成目标分支为 `main`。项目包含 Qt 桌面前端、独立采集进程、1553B 软件监控输入、原始归档、参数解码、时间关联与离线分析。

在插件中心启用所需插件，点击顶部“开始采集”后开始记录；底部持续显示状态与实际保存路径。数据关联页支持多路参数、命名公式和随时间变化的结果。内置演示和 1553B 验证使用软件合成数据，采集卡 SDK、ICD 和实际设备负载验收待确定。

源码项目位于 [`avionics-backend/`](avionics-backend/)，包括：

- [`frontend/`](avionics-backend/frontend/)：Qt 工作台、插件中心、监测、数据关联和离线分析。
- [`asset/`](avionics-backend/asset/)：统一美术素材，包含功能图形、背景、场景、空状态和流程示意；提示词与用途保存在 `prompts.json`。
- [`src/`](avionics-backend/src/) 与 [`plugins/`](avionics-backend/plugins/)：C++ 采集宿主、隔离进程、协议与参数处理、存储以及采集插件。
- [`config/`](avionics-backend/config/)：字典、规则、时钟与协议输入配置。
- [`docs/`](avionics-backend/docs/)：使用、架构和验收边界。

- [协议抽象类与选择策略](avionics-backend/docs/protocol-abstractions.md)
- [抽象接口头文件](avionics-backend/include/core/protocol_interfaces.hpp)
- [协议数据类型](avionics-backend/include/core/protocol_types.hpp)
- [构建、运行与已有功能](avionics-backend/README.md)
- [当前系统架构](avionics-backend/docs/architecture.md)
- [Qt 工作台操作](avionics-backend/docs/qt-frontend.md)
- [1553B 软件验证](avionics-backend/docs/mil1553b-software.md)

在 Windows / MinGW 环境构建并测试：

```powershell
cd avionics-backend
.\scripts\build-mingw.ps1 -RunTests
```

脚本默认使用 `C:\msys64\mingw64\bin`，需要 PATH 中可用的 CMake 3.24+、Ninja。已在 Windows / g++ 15.2 验证；Linux 构建命令见项目说明，尚未在 Linux 实测。构建产物、运行记录和本地历史快照不纳入 Git。
