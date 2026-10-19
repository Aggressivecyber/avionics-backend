# Qt 地面试验工作台 · v0.4

前端采用 Qt Widgets / 官方 Python 绑定 PySide6 6.9.2。界面和 C++ 采集宿主分属两个进程，通过 `QProcess` 的异步信号交换控制与显示数据；采集宿主继续为每个来源创建独立的 SDK 工作进程。界面没有加载设备插件，也不在 Qt 事件线程等待设备启停。

## 本机启动

源码、输出及临时文件保留在 D/E 盘。现有环境为 `D:/anaconda/python.exe`，Qt 版本通过实际创建 `QApplication` 验证为 6.9.2。

```powershell
.\scripts\start-frontend.ps1 -Python D:/anaconda/python.exe -BinDir D:/avionics-builds/ground-backend-v03-final/bin -RuntimeDir C:/msys64/mingw64/bin -DataDir E:/avionics-data/ground-station
```

发布包包含 `Start-AEROBUS.cmd`、界面源码和后端运行库，默认复用上述 D 盘 Python / Qt。可传 `-Python D:/other-env/python.exe -DataDir E:/other-data`。Qt/Python 运行时没有打入本次便携包；依赖版本见 `frontend/requirements.txt`。

## 操作流程

1. 选择字典/规则配置与 D/E 盘数据目录，填写试验名称，点击“新建会话”。原始归档、生命周期日志、配置副本与参数/事件结果使用同一会话名称。
2. 点击“添加”，填写逻辑源 ID、插件 DLL 和单行配置。源 ID 与字典中的 `source` 对应。选择来源后可启动、停止、移除或替换插件。
3. 参数表显示工程量、单位、质量、原始记录索引与来源代次。选择参数查看最近趋势；无效质量样本断开曲线，替换或重启产生新代次时清空旧曲线。
4. 双击参数或事件查看会话身份、配置 SHA-256、原始记录位置、来源代次与事件证据。过滤仅影响参数显示。
5. 点击“结束并封存”，等待后端回执。窗口关闭也先停止来源并等待封存。采集降级、记录失败和分析不完整分别显示；已有 SDK 故障不会因为移除来源而显示成成功采集。
6. 在“离线分析”选择归档，先校验完整性或直接执行重新分析。新输出目录必须不存在。完整性错误显示失败，并清除之前的成功结果。离线分析期间窗口继续响应，结束后可以关闭。

“启动合成演示”创建四个明确标识的模拟源：`sensor_a`、`sensor_b`、`control`、`feedback`。默认示例字典描述合成载荷；真实总线、SDK 和 ICD 选定后接入对应插件与字典。

## 显示与记录边界

- 显示刷新间隔 250 ms。每次最多返回最近 512 个参数样本、128 个关联事件，并通过游标报告界面没有取得的历史条数。界面保留 512 个趋势点、4096 个参数状态、200 个事件和 300 行操作日志。
- 显示缓冲覆盖只影响界面历史，不影响完整的参数/事件 JSONL 或原始归档。显示历史计数与采集拒绝计数分开呈现。离线结果也只读取有界文件末尾。
- 两秒没有新状态会显示“等待后端响应”；命令没有通过就显示错误。后端异常退出且没有封存回执时，界面不确认记录完整。
- 趋势显示采用通过质量检查的映射事件时间。界面不会生成设备时间水位、自动推断硬件同步或补造规则结果。
- 关闭界面期间等待后端排空和封存。若外部强制结束后端，原始文件可能未封存，完整性校验会拒绝该文件。

## 控制协议 1

启动：`bus_backend --control NEW_ARCHIVE CONFIG_INI`。命令行在 Windows 从 UTF-16 转成 UTF-8；控制管道使用 UTF-8。

标准输入每行一条命令。请求编号为正整数，字符串使用 C++ `std::quoted` 的双引号及反斜杠转义，禁止内嵌换行。控制行上限 64 KiB。处理后只向标准输出写一条紧凑 JSON，诊断使用标准错误。

```text
1 add "sensor_a" "E:/plugins/含 空格的插件.dll" "period_ms=20"
2 start "sensor_a"
3 snapshot 0 0
4 stop "sensor_a"
5 replace "sensor_a" "E:/plugins/new.dll" "period_ms=20"
6 remove "sensor_a"
7 quit
```

启动时发送 `{"schema":1,"type":"ready",...}`。普通响应包含 `request`、`ok`，以及成功时的 `data` 或失败时的 `error`。`snapshot` 的两个参数为已经取得的样本和事件游标；响应包含记录/分析状态、来源状态、增量参数/事件和新的游标。来源操作在完整解析参数之后执行。

`quit` 的命令响应只表示接受结束请求。只有收到 `type=closed` 的最终会话结果，并且后端正常结束，才能判断记录封存及采集/分析状态。标准输入关闭也会走后端正常排空流程。

## 自动化验收

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:TEMP = 'E:/avionics-data/qt-test-temp'
$env:TMP = $env:TEMP
$env:PATH = 'C:/msys64/mingw64/bin;' + $env:PATH
D:/anaconda/python.exe -B frontend/test_frontend.py --bin-dir D:/avionics-builds/ground-backend-v03-final/bin --output E:/avionics-ui/verification
```

每次测试创建新的 `run-*` 目录，保留 `receipt.json` 和三个实际 Qt 窗口截图。Windows 的 offscreen 插件没有系统字体数据库；测试显式读取已经安装的微软雅黑和 Segoe UI 字体。实际桌面程序使用 Windows 平台插件的字体系统。

四组 Qt 测试验证：

1. 四源实时显示、显示缓冲覆盖与可见缺口、带中文及空格路径的插件替换、源启停/移除、记录封存、在线与离线参数/事件文件 SHA-256 完全一致、损坏归档拒绝。
2. SDK 加载卡死约五秒期间的 Qt 计时器响应、异步 SDK 故障可见、最终采集降级与原始记录封存。
3. 关闭活动窗口时正常封存、输入源 ID 校验、后端启动失败与 D/E 写入边界。
4. 后端异常退出不会显示成成功封存，未封存的原始文件被完整性校验拒绝。

也可纳入 CTest：配置 `-DAVIONICS_TEST_QT_FRONTEND=ON -DPython3_EXECUTABLE=D:/anaconda/python.exe`，随后运行常规 `ctest --test-dir BUILD --output-on-failure`。现有五组 C++ 测试继续执行。Qt 测试结果只证明上述软件场景；设备通道数、连续运行时长、峰值吞吐和真实驱动故障需要在选型后验收。
