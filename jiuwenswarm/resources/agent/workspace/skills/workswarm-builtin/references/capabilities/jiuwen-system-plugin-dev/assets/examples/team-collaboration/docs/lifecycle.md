# 生命周期：锚点、端口、释放

## 1. 为什么锚点必须是 Handler 对象

宿主（`0.2.5.beta1`）加载扩展的调用链：

```text
openjiuwen/core/extension/runtime.py:836   extension = factory()        # 只是局部变量
                                        :843   register(registrar)       # 返回即出作用域
                                        :97    register_event_handler(...)  # 存 handler 引用
                                        :126/:129  handler=handler          # bound method
                                        :567-569 / :592  _tracked_handler() 挂到 EventBus
```

- `create_extension()` 的返回值**不被宿主保留**（`ActivationCandidate`/`PrepareResult` 里没有 instance 字段），
  `register()` 返回后实例就没有外部强引用了。
- `register_event_handler()` 存的是 **bound method**；bound method 通过 `__self__` 强引用**它所属的 Handler 对象**，
  与 extension 实例无关。
- 真实持有者是 **EventBus**（`_tracked_handler()` 把 bound method 包进闭包挂进 handler 表），
  因此 Handler 的存活期 = 该 declaration 的存活期。

实测两种锚点的差异（探针）：

| 锚点 | 结果 |
|---|---|
| extension 实例 | register 后 **≈2ms** 触发 finalize，**≈0.12s** 端口已关闭 → 前端拿到的 `src` 指向死端口（白屏） |
| Handler 对象（本包采用） | 端口随 activation 存活；`reload`/`uninstall` 时随 handler 释放 |

## 2. 本包的实现

- `TeamCollabVizExtension.__init__` 只创建 `TeamVizHandlers`；`register()` 把 Handler 的两个 bound method
  交给 `registrar.register_event_handler(...)`。
- `TeamVizHandlers.__init__` **先** `lifecycle.start_service(self, context)`（**监听先于任何渲染**，R1），
  再对外提供 `on_ui_ready` / `on_team_monitor_event`。
- `lifecycle.start_service(owner, context)`：
  - `ThreadingHTTPServer(("127.0.0.1", 0), ...)` → **端口由内核分配**，`port = httpd.socket.getsockname()[1]`（R2）；
  - `weakref.ref(owner)` + `weakref.finalize(owner, ...)` 作为释放锚点；
  - 看门狗线程每 **0.2s** 检查 owner 弱引用是否失效（`WATCHDOG_INTERVAL_SECONDS = 0.2`，R5 要求 ≤1s）；
  - 失效即 `shutdown() + server_close()`，`port` 归零。
- 服务实例登记在 `sys.modules["_tcv_local_services"]` 这一**非包前缀**模块里，
  这样 `reload` 的 `_release_modules()` 把 `team_collab_viz.*` 从 `sys.modules` 摘掉时，
  注册表本身不会随之丢失（否则释放钩子没了，端口会泄漏）。
- **禁止**把需要被回收的对象挂在模块级全局变量上：`_release_modules()` 只摘 `sys.modules`，
  若还有外部引用，回收时机不可控，端口可能长时间不释放。

## 3. 端口释放口径（契约 §5.5 R4/R8）

| 模式 | 语义 | 要求 |
|---|---|---|
| **A（本包唯一实现）** | activation 失效（`reload` / `uninstall` / 实例被 GC） | **≤5s 内该端口无 LISTEN**；实测**21–106ms**（探针）；本包隔离实测 `del ext; gc.collect()` 后 **300–600ms** 无 LISTEN（差异来自 GC 与解释器调度） |
| **B（未实现）** | 同实例正常端口切换 | 允许 30s 宽限，且端口必须出现在 `/health.draining_ports` |

本包遵循 **R3：一实例一端口**，因此 `draining_ports` 恒为 `[]`（字段存在，便于验收对齐）。

**R6 绑定期失败**：`bind` 抛 `OSError` 时记录 `context.bind_error`，
`/health.ok=false` 且带 `bind_error`，`on_ui_ready` 直接返回 `accepted=false, status="render_failed"`
（不渲染指向空端口的 `src`）。

## 4. 渲染幂等（R7）

`ui.ready` 每次都按 `instance_key` 重新 `ops.ui.render(...)`，可被重复调用；
`src` 始终由当前 `context.port` 生成，所以端口失效后重渲染会自然修正（但按 R3 不会出现新端口）。
