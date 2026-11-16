# AEROBUS

**面向机载总线数据的采集、回放与分析工作台**

AEROBUS 将 Qt 桌面界面、C++ 采集后端和协议处理组件组合在一起，支持从模拟数据采集到原始报文归档、参数解码、关联分析和验收报告的工作流程。当前源代码版本为 **v0.8.0**。

> 当前 1553B 功能使用合成监测记录进行软件验证。真实采集卡、SDK、设备 ICD、时钟同步及实际负载仍需针对所选设备适配和验收。

## 功能概览

| 功能 | 说明 |
| --- | --- |
| 桌面工作台 | 使用 Qt 界面管理项目、采集源、插件、实时监测、历史记录和数据分析。 |
| 数据采集与回放 | 支持模拟源、场景源及归档回放；采集源运行在独立工作进程中。 |
| 1553B 软件验证 | 对合成记录进行消息解析、字典选择与参数解码，覆盖 BC→RT、RT→BC、RT→RT 及部分广播消息。 |
| 归档与恢复 | 保存原始报文和会话信息，提供完整性校验、分卷、磁盘空间预警及中断恢复能力。 |
| 分析与验收 | 支持参数、事件和公式关联分析；可运行合成场景并生成验收报告。 |
| 插件扩展 | 通过采集插件和协议接口扩展数据源与协议处理。 |

## 快速开始

以下示例使用 Windows、MinGW 和 PowerShell，源代码、构建文件与运行数据均放在 E 盘。请按实际安装位置调整编译器和 Python 路径。

### 1. 获取源码并构建后端

需要 Git、CMake 3.20 或更新版本、Ninja，以及支持 C++20 的 MinGW 编译器。

```powershell
cd E:/
git clone https://github.com/Aggressivecyber/avionics-backend.git
cd avionics-backend/avionics-backend

$env:PATH = 'C:\msys64\mingw64\bin;' + $env:PATH
cmake -S . -B E:/avionics-builds/aerobus -G Ninja -DCMAKE_BUILD_TYPE=Release `
  -DCMAKE_C_COMPILER=C:/msys64/mingw64/bin/gcc.exe `
  -DCMAKE_CXX_COMPILER=C:/msys64/mingw64/bin/g++.exe
cmake --build E:/avionics-builds/aerobus --parallel 2
ctest --test-dir E:/avionics-builds/aerobus --output-on-failure
```

后端程序和插件生成在 `E:/avionics-builds/aerobus/bin`。测试结果反映当前运行环境中的软件验证，不代表真实设备验收。

### 2. 启动桌面工作台

在 E 盘创建 Python 环境并安装 PySide6，随后从项目的 `avionics-backend` 目录启动：

```powershell
python -m venv E:/avionics-env
E:/avionics-env/Scripts/python.exe -m pip install -r frontend/requirements.txt

.\scripts\start-frontend.ps1 `
  -Python E:/avionics-env/Scripts/python.exe `
  -BinDir E:/avionics-builds/aerobus/bin `
  -RuntimeDir C:/msys64/mingw64/bin `
  -DataDir E:/avionics-data/ground-station
```

在工作台中创建项目，准备模拟采集源，再点击“开始采集”。1553B 示例及操作范围见 [1553B 软件验证](avionics-backend/docs/mil1553b-software.md)。

### 3. 使用命令行

构建后可以查看版本、检查归档或运行离线分析：

```powershell
E:/avionics-builds/aerobus/bin/bus_backend.exe --version
E:/avionics-builds/aerobus/bin/bus_analyze.exe --inspect E:/bus-runs/example.avbus
E:/avionics-builds/aerobus/bin/bus_analyze.exe --analyze config/correlation-demo.ini E:/bus-runs/example.avbus E:/bus-runs/example-analysis
```

其中 `example.avbus` 需要替换为已有归档。采集控制、归档文件和退出状态的详细说明见 [后端使用文档](avionics-backend/README.md)。

## 项目结构

```text
avionics-backend/
├── frontend/    Qt for Python 桌面工作台
├── src/         采集、协议处理、分析和存储实现
├── include/     核心接口与插件 API
├── plugins/     模拟、场景、回放和 1553B 模拟插件
├── config/      示例字典、规则与采集配置
├── tests/       后端测试
├── scripts/     构建、启动、验证和打包脚本
├── docs/        架构、协议和验收文档
└── asset/       桌面界面素材
```

## 文档与验证

- [后端构建、运行和打包](avionics-backend/README.md)
- [系统架构](avionics-backend/docs/architecture.md) · [Qt 工作台](avionics-backend/docs/qt-frontend.md)
- [协议抽象](avionics-backend/docs/protocol-abstractions.md) · [插件契约](avionics-backend/docs/plugin-contract.md)
- [1553B 软件验证范围](avionics-backend/docs/mil1553b-software.md) · [场景验收](avionics-backend/docs/scenario-acceptance.md)
- [录制分卷和中断恢复](avionics-backend/docs/recording-recovery.md) · [产品验收边界](avionics-backend/docs/product-acceptance.md)

仓库提供 Windows 和 Linux 的后端 CI 配置；实际运行结果请查看 [GitHub Actions](https://github.com/Aggressivecyber/avionics-backend/actions)。运行记录、构建产物和采集归档不纳入 Git。
