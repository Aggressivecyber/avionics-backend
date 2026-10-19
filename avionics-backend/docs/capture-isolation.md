# 采集故障隔离与监督

每个逻辑来源有独立 `bus_capture_worker` 进程。宿主加载 `CaptureAdapter`，工作进程加载 `PluginModule` 和设备 DLL。插件加载、配置、启动、停止、销毁都在这一边界内，预检也不把 SDK 代码带入宿主。

命令有应答和截止；状态约每 20 ms 更新一次，因此公开状态是最近的采集快照。工作线程异常保留 `last_error`，用户态进程退出保留退出码，命令挂起保留超时操作编号。错误同时写入生命周期日志，移除或重建实例后整次会话仍标记为降级。恢复由明确的 replace/start 操作进行，没有自动重试隐含的数据段。

Windows 使用两条独立命名管道：命令和数据分别单向传输，避免同步双工句柄把读操作和写操作串行阻塞。宿主端使用重叠 I/O 和可取消读取，单条消息上限为最大载荷加 8192 字节，配置字符串上限 16 KiB。消息长度、字段边界和 IPC 协议版本在进程边界校验。

启动使用限定的句柄列表，Job 在创建时绑定并启用最后句柄关闭后终止，避免先启动再绑定产生孤立工作进程。实际验收覆盖宿主被直接终止后子进程退出。实现依据：[受控句柄继承](https://learn.microsoft.com/en-us/windows/win32/procthread/inheritance)、[进程属性与 Job 列表](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)、[Job 生命周期](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。

IPC 满时反压采集回调；宿主另有按来源和全局的原始队列预算。插件传输接纳、宿主入口接纳和落盘完成分别统计，不能互相替代。真实 SDK 回调是否允许等待、设备缓存和驱动是否会丢失数据，需要设备负载验收。

Linux 路径使用本地 socket、独立进程组及父进程退出信号；已提供实现和 CI 任务，本轮未原生运行。Windows 用户态隔离已实测；驱动内核挂起、设备复位、PCIe 总线故障和系统断电不由这个进程边界保证。

正常停止先等 SDK 回调结束，再以宿主原始 recorded 数量排空。分析另行完成或明确失败。强制终止可能丢失采集进程中尚未交付的 IPC/SDK 数据，因此会话必须降级，不能宣称这一段无丢失。
