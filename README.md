# 机载总线监控后端

C++20 / g++ 的面向对象后端工程，源码位于 [`avionics-backend/`](avionics-backend/)。提供模拟采集、采集插件热替换、原始记录与回放、配置化参数解码及关联分析。

本轮完成多协议扩展的抽象接口：默认自动识别，允许按来源和通道手动指定协议。具体识别算法、路由器和真实协议解析器留待后续实现；当前没有真实硬件依赖。

- [协议抽象类与选择策略](avionics-backend/docs/protocol-abstractions.md)
- [抽象接口头文件](avionics-backend/include/core/protocol_interfaces.hpp)
- [协议数据类型](avionics-backend/include/core/protocol_types.hpp)
- [构建、运行与已有功能](avionics-backend/README.md)
- [后端架构](avionics-backend/docs/architecture.md)

在 Windows / MinGW 环境构建并测试：

```powershell
cd avionics-backend
.\scripts\build-mingw.ps1 -RunTests
```

脚本默认使用 `C:\msys64\mingw64\bin`，需要 PATH 中可用的 CMake 3.24+、Ninja。已在 Windows / g++ 15.2 验证；Linux 构建命令见项目说明，尚未在 Linux 实测。构建产物、运行记录和本地历史快照不纳入 Git。
