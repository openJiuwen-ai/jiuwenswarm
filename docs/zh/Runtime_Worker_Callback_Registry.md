# Runtime Worker 回调注册表的事件循环归属

Code 会话在线程中使用临时事件循环初始化，常驻 Worker 在业务事件循环中处理配置热更新。两条路径都会访问 `Runner.callback_framework`。SDK 注册表使用 `asyncio.Lock`；并发注册使锁绑定到某个初始化循环后，另一个循环的注册或注销操作可能报 `is bound to a different event loop`，导致 `agent.reload_config` 重试失败。

Worker 在构造 Server、加载扩展和初始化智能体之前，为当前 `Runner.callback_framework` 实例安装事件循环转发：

- 异步注册、注销接口由 Worker 业务事件循环执行；初始化循环异步等待结果，取消会传递给转发的任务。
- 同步注册、注销、过滤器、hook 和查询接口在同一个循环执行；初始化线程等待返回值或原始异常。装饰器通过同步注册接口使用同一条路径。
- 已在业务循环中的调用直接执行；回调触发和用户回调的执行语义保持 SDK 现有行为。

该机制保留现有初始化线程，耗时的模块加载和初始化工作仍在线程中运行，只将注册表操作转发到业务循环。不要把耗时初始化或用户回调放进注册表操作中。

注册表归属与 Worker 进程生命周期一致。Server 停止、SDK 清理结束后，Worker 关闭转发入口，取消尚未完成的转发任务，释放等待中的线程，并拒绝迟到的注册。构造失败时，尚未交给 Runtime 的入口由启动代码关闭；已经构造的 Server 由原有 Runtime 清理流程负责。

本修复不调整 Core 源码、依赖版本或 SDK 私有锁，也不修改 Gateway 的重试策略。入口只在 Server Worker 启动时安装，不能作为 SDK 在任意进程、任意事件循环之间共享对象的通用支持。每个新 Worker 应使用新的进程和注册表实例；不要在一个进程中将同一实例重新绑定到另一个事件循环，也不要从初始化线程直接修改 SDK 暴露的底层注册表字典。

英文说明：[Runtime Worker callback registry ownership](../en/Runtime_Worker_Callback_Registry.md)。
