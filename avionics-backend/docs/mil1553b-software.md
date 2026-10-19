# MIL-STD-1553B 软件验证

首个协议目标为 MIL-STD-1553B，当前没有采集卡、SDK 或设备 ICD。本轮从总线监控端视角验证协议处理，使用合成输入和示例参数字典。

## 分层与范围

- `plugins/mil1553b_simulated/`：ABI 2 模拟采集插件，只生成监控输入，不执行工程量解码或关联规则。
- `include/core/mil1553b.hpp`、`src/decoder/mil1553b.cpp`：实现已有协议接口，解析消息并接入独立参数接口。
- `config/mil1553b-demo.ini`：显式输入格式、自动/手动选择、RT/子地址/方向、示例工程量、时钟和规则。
- 原始记录、工作进程隔离、事件时间处理、离线重算及 Qt 插件中心复用现有实现。启用插件只准备来源，点击“开始采集”后才生成输入。

首版验证 BC→RT、RT→BC、RT→RT 以及基础 1553B 可选广播数据消息，常规数据字数 1..32；编码字段 0 表示 32。检查字序、数量、RT 地址、奇校验和捕获格式。状态字 Busy、Message Error、Subsystem Flag、Terminal Flag 保留并阻止其数据作为有效工程量使用。模式指令及RT→RT的Busy/Message Error响应明确返回 Unsupported。

这是基础消息格式的软件子集，不是 Notice II 应用规范或完整标准符合性验收。消息格式及状态位以 [MIL-STD-1553B 原文的图3、4.3.3.5与4.3.3.6（NASA存档）](https://nepp.nasa.gov/docuploads/43745C0A-323E-4346-A434F4342178CD0E/MIL-STD-1553.pdf) 为依据。没有模拟电压、Manchester 波形、真实字间隔、RT 响应时间或设备行为。

16位状态值中，Broadcast Command Received为`0x0010`、Busy为`0x0008`、Subsystem Flag为`0x0004`。广播接收标记不代表Busy；Busy或Message Error的RT→BC响应仅含状态字。Subsystem Flag、Busy、Message Error和Terminal Flag阻止其工程数据参与有效计算；保留完整状态字供溯源。

## 成功标准

1. 用独立固定十六进制样例验证正常消息、32 字边界、广播无接收状态字及 Busy 响应；异常输入得到明确诊断且不产生有效工程量。
2. 同一物理通道的不同 RT、子地址和方向能正确选择字典；A/B 总线标记单独保留，不占用逻辑通道编号。
3. 不同来源、通道和代次的协议状态隔离；手动指定协议后，坏帧不能触发协议切换。
4. 模拟插件通过独立采集进程运行，停止、替换和回放保留现有生命周期及证据身份语义。
5. 公开命令行采集后，归档通过封存校验；在线与离线的参数及事件逐字节一致，异常诊断也可复现。
6. Qt 中启用1553B插件后帧数仍为零，点击“开始采集”后出现数据；结束后取得封存回执。全部既有回归通过。

实现、构建和验收文件均放在 D/E 盘。软件通过和真实设备验收分别报告。

## 监控记录输入格式

程序内部协议编号为 `BUS_PROTOCOL_MIL1553B=1553`，此编号由本项目分配。每个 `RawFrame` 包含一条完整事务，不跨记录组帧。输入表示必须显式声明为 `mil1553b.monitor-v1`；自动模式检查项目封装特征，手动模式仍校验内容。

| 字节位置 | 内容 |
| --- | --- |
| 0..7 | ASCII `M1553B01` |
| 8 | A/B总线：0为A、1为B；逻辑通道仍使用 `RawFrame.channel` |
| 9 | 捕获字数量1..36，包含命令、数据和状态字 |
| 10..11 | 保留为0 |
| 12起，每字4字节 | u16小端捕获值、u8同步类型（0命令/状态，1数据）、u8实际奇校验位（0或1） |

命令和状态使用相同同步类型，根据消息位置区分。协议解析后的数据载荷按每字高字节在前排列；字段的 `bit_offset` 从这份数据载荷起算，不包括命令和状态。真实SDK若不提供同步或校验观测，需要另行定义输入表示，不得补造观测。当前没有微秒级字时间、波形或消息切分验收。

## 字典与诊断

`[protocol <id>]` 的 `source` 默认采用节ID，也可显式填写；`channel`、`protocol=1553`、`capture_representation` 必填，`selection` 默认为 `auto`，也支持 `manual`。每个来源/通道只能绑定一次。回放来源需与输入绑定同名；其原来源身份继续隔离解析器状态，并继承该逻辑来源的输入格式与选择策略。

1553B参数必须明确填写 `rt_address=0..31`、`subaddress=1..30` 和 `direction=receive|transmit`。RT31只允许receive。RT→RT中的两个端点均可映射同一份数据载荷，不把RT或子地址编码到物理通道编号。示例参数为用户演示定义，不对应真实飞机参数。

每个输入记录产生 `mil1553b.protocol_status.ch<通道>` 枚举参数，保存消息类别、A/B总线、状态字或错误原因，并保留会话、配置摘要与原始记录位置。编号0为完整消息，1为畸形输入，2为暂未支持，3为无法确定路由，4为捕获或RT状态无效。非0诊断不推进业务事件水位。无法信任命令字时，该来源/通道的已配置工程参数全部输出无效样本，以中断旧关联基线；不把缺口拼成正常响应。

## 重复验收与启动

```powershell
$env:PATH = 'C:/msys64/mingw64/bin;' + $env:PATH
$env:TEMP = 'E:/avionics-data/1553b-temp'
$env:TMP = $env:TEMP
$env:QT_QPA_PLATFORM = 'offscreen'
ctest --test-dir build-1553b --output-on-failure
D:/anaconda/python.exe -B scripts/verify_mil1553b.py --build build-1553b --output E:/avionics-data/1553b-verification
```

`verify_mil1553b.py` 使用公开结构化控制接口，等待实际记录和响应后再替换来源，避免把插件启动耗时当成采集时长。脚本验证奇校验错误、两次采集代次、独立SHA-256核对、原始记录回放、证据身份以及两轮在线/离线逐字节一致。回放仍为recorded时钟，示例不会把它冒充host时钟；直接离线重算原始归档可重现原分析。

启动工作台：`scripts/start-frontend.ps1 -Python D:/anaconda/python.exe -BinDir <构建目录>/bin -DataDir E:/avionics-data/1553b-workbench`。在插件中心启用“1553B软件验证插件”，再点击“开始采集”。示例50ms关联窗口针对宿主软件生成的记录调度，不是1553B物理层时序要求。
