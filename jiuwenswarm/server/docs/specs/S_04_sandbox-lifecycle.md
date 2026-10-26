# Sandbox 生命周期

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_04 / sandbox-lifecycle |
| 关联模块 | jiuwenswarm/server/{sandbox_lifecycle.py, sandbox_config_rpc.py, sandbox_policy_render.py, sandbox/jiuwenbox_runner.py, sandbox/port_util.py, handlers/sandbox.py, agent_ws_server.py（_bootstrap_internal_jiuwenbox）} |
| 最近一次修订日期 | 2026-10-15 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本规约管什么

- sandbox auto-start 条件链（7 步 gate）：enterprise 跳过 → macOS 跳过 → enabled=true → startup_mode=internal → 平台是 linux/win → 非 yuanrong → policy_file 存在。
- JiuwenBoxRunner 单例：ensure_running / shutdown / get_stderr_tail；host + port + startup_mode + policy_path + extra_env 入参。
- 端口分配：handlers/sandbox.py 的 `allocate_internal_jiuwenbox_port` → preferred_port 冲突自动回退 → AgentWebSocketServer 进程内 `_is_tcp_port_bindable` / `_pick_free_tcp_port`（AgentServerServices 门面暴露）。
- sandbox 端点持久化：auto-start 成功 → `update_sandbox_endpoint(url, sandbox_type, startup_mode="internal", policy_file=effective_policy_file)`；`sandbox.enabled` 同步置 `True`。
- Windows 平台 sandbox policy runtime 副本：sandbox_policy_render.ensure_copy_exists() 为 box-server 提供 runtime 级覆盖文件。
- shutdown 两步清理（app_agentserver.py finally）：先 DELETE 远端沙箱（HTTP → box-server）→ 再停 box-server 子进程（JiuwenBoxRunner.stop()）；企业版 sidecar 跳过 step 1。
- `agent_ws_server._bootstrap_internal_jiuwenbox` 的 best-effort 契约：失败一律 warning，绝不阻断 AgentServer 启动。

### 本规约**不**管什么

| 子主题 | 归属 spec |
|---|---|
| AgentManager / AgentWarmPool / agent_adapter | [[S_03]] runtime-agent-management |
| HANDLERS 注册表 / 默认路径 | [[S_02]] request-pipeline-dispatch |
| im_connector Plugin、im_hosting 托管 | [[S_05]] im-channel-integration |

## 不变量

1. **auto-start 条件链严格按顺序**：gate 自上而下命中失败即 return，不往后看；`enterprise=True` → 整条跳过，sidecar 由 K8s 提供。
2. **startup_mode 必须显式配置**：`get_sandbox_startup_mode_explicit()` 读 yaml 字段缺失返回 None，走默认 host 模式不拉起 box-server；**不要**用 `get_sandbox_startup_mode()`（它在字段缺失时回落默认 internal，会让没在用沙箱的用户升级后突然多出进程）。
3. **enabled=false 优先级最高**：Windows 平台 `sandbox.runtime.enabled=False` 时整条跳过 auto-start；即使 startup_mode=internal 也不拉（opt-in 默认 false）。
4. **JiuwenBoxRunner 单例**：`JiuwenBoxRunner.instance()` 是进程内唯一实例，构造参数 host/port/startup_mode/policy_path 首次生效；ensure_running 后续调用幂等。
5. **shutdown 两步顺序不能颠倒**：先 DELETE 远端沙箱（box-server 活着才能响应）→ 再停 box-server 子进程。反过来 DELETE 必败、沙箱没清。
6. **Windows box-server 需 JIUWENBOX_BUNDLED_PYTHON / JIUWENBOX_VENV_DIR / JIUWENBOX_RUNNER_PYTHON 三个 env**：runner 必须是标准 CPython（非 uv trampoline/venv launcher）——`_is_std_cpython()` 判定 `python.exe` 父目录有 `python3*.dll` 且不在 Scripts/ 子目录。
7. **yuanrong 沙箱不走本地 box-server**：`sandbox.type=yuanrong` auto-start 跳过（YuanRong 用 YR_* env + yr.init）。
8. **sandbox auto-start 永远不阻断启动**：`_bootstrap_internal_jiuwenbox` 内部 `except Exception: logger.warning(...); return`；失败留给 TUI `/sandbox enable` 重试。

## 接口契约

### JiuwenBoxRunner（单例）

```python
class JiuwenBoxRunner:
    @classmethod
    def instance(cls) -> "JiuwenBoxRunner": ...

    async def ensure_running(self, *, host: str, port: int, startup_mode: str,
                              policy_path: str, extra_env: dict[str, str] | None = None,
                              timeout: float = 180.0) -> bool: ...
    async def stop(self) -> None: ...
    def get_stderr_tail(self, lines: int = 10) -> str: ...
    def get_owned_endpoint(self) -> str | None: ...  # internal 模式下返回本进程拉起的 endpoint url
```

### auto-start 条件链（`AgentWebSocketServer._bootstrap_internal_jiuwenbox`）

```
1. is_enterprise()？→ skip (enterprise 用 sidecar)
2. sys.platform 不是 linux/win32？→ skip
3. (win32 AND sandbox.runtime.enabled=False)？→ skip (opt-in 默认 false)
4. explicit_mode = get_sandbox_startup_mode_explicit()
   → None (yaml 字段缺失)？→ skip (走默认 host 模式)
   → explicit_mode != "internal"？→ skip (external 由用户拉)
5. endpoint = get_sandbox_endpoint() → sandbox.type == "yuanrong"？→ skip
6. policy_path = resolve_sandbox_policy_path(DEFAULT_SANDBOX_POLICY_FILE)
   → 无法解析到存在的文件？→ warning + return
7. allocate_internal_jiuwenbox_port → port = runner.ensure_running(...timeout=180)
   → ok → update_sandbox_endpoint(url, ...) + update_sandbox_runtime({"enabled": True})
   → not ok → stderr tail + warning
```

### shutdown 两步清理（`app_agentserver.main()` finally）

```python
# step 1: DELETE 远端沙箱（HTTP → box-server；必须 box-server 活着）
if not is_enterprise():
    await asyncio.to_thread(shutdown_jiuwenbox_sandboxes)

# step 2: 停 box-server 子进程
await JiuwenBoxRunner.instance().stop()
```

## 执行路径 / 装配关系

### P1：启动时 auto-start

```
AgentWebSocketServer.start(listen=True)
  → legacy_serve 开 WS 端口
  → interface_deep 预热（阶段 1+2 fire-and-forget）
  → _bootstrap_internal_jiuwenbox()
     → 条件链 1-7 逐一 gate
     → win32？ensure_copy_exists()（runtime policy 副本）
     → win32？注入 JIUWENBOX_BUNDLED_PYTHON / JIUWENBOX_VENV_DIR / JIUWENBOX_RUNNER_PYTHON env
     → runner.ensure_running(timeout=180)
     → ok → update_sandbox_endpoint + update_sandbox_runtime({"enabled": True})
     → exception → logger.warning + return（不阻断）
  → event_loop_lag_monitor 启动
  → im_hosting.start()（非 enterprise）
```

### P2：TUI /sandbox enable 显式拉起（绕过条件链）

```
handlers/sandbox.handle_command_sandbox(ctx, params={"action": "enable"})
  → 跳过 enabled / startup_mode gate（TUI 显式拉就是用户意图）
  → 调用 AgentWebSocketServer 侧同一 ensure_running + recreate_agent
```

### P3：进程退出清理

```
app_agentserver.main() finally
  → usage_reconciler.stop
  → MCP registry stop
  → HTTP server stop
  → server.stop()  # AgentWebSocketServer.stop
  → sandbox shutdown_jiuwenbox_sandboxes（step 1）
  → JiuwenBoxRunner.stop()（step 2）
  → perf flush
  → observability shutdown
```

### P4：sandbox_config_rpc 同步 RPC handler

```python
# sandbox_config_rpc 在 AgentWebSocketServer.start() 里 register_main_loop(loop)
# 工作线程里的同步 handler → run_coroutine_threadsafe 投递到主 event loop
# 替代废弃的 asyncio.get_event_loop().create_task()
```

## 边界 case 清单

| 场景 | 现行为 | 风险 | 测试覆盖 |
|---|---|---|---|
| enterprise=true | 整条 auto-start 跳过 | sidecar K8s 启动顺序未知 | enterprise 跳过 gate |
| macOS | 整条跳过 | jiuwenbox 依赖 Linux bwrap/Landlock + Windows WFP | 非 linux/win32 gate |
| sandbox.runtime.enabled=false（win32） | 整条跳过 | opt-in 默认 false，开箱不拉起 | enabled gate |
| startup_mode yaml 字段缺失 | explicit_mode=None → skip | 走默认 host 模式，不拉 box-server | explicit gate |
| startup_mode=external | skip | 用户自行拉起 box-server | external gate |
| sandbox.type=yuanrong | skip | YuanRong 不用本地 box-server | yuanrong gate |
| policy_file 解析失败 | warning + return | 用户可能没显式写 policy_file，走 DEFAULT_SANDBOX_POLICY_FILE 也可能解析失败 | policy gate |
| preferred_port 已被占 | allocate_internal_jiuwenbox_port 自动回退 → update_sandbox_endpoint(url) 带实际端口 | box-server 仍能拉起，TUI 显示真实端口 | 端口回退 |
| box-server ensure_running timeout=180s | 超过时间 → ok=False → stderr tail + warning | 不阻断 AgentServer 启动 | timeout gate |
| shutdown 先停 box-server 再 DELETE | DELETE 全失败 | enterprise sidecar 秒退也会失败，外部 box-server 必须先 DELETE | shutdown 两步顺序 |
| win32 Python 非 CPython（uv trampoline） | _is_std_cpython 返回 False → 手动探测候选路径 | runner 拿不到合法 python → jbx-sandbox 跑不了 | Windows Python 探测 |

## 修改检查单

- **改 auto-start 条件链** → 7 步 gate 自上而下检查（enterprise → 平台 → enabled → explicit_mode → sandbox_type → policy → runner）→ 新增 step 必须在 AgentWebSocketServer.start() 里同步插入 → 确认不打破 N8（best-effort）。
- **改 `get_sandbox_startup_mode_explicit`** → 确认不回落默认 internal；默认值语义变化会让没在用沙箱的用户升级后拉起 box-server → 同步所有 auto-start 条件链引用。
- **改 JiuwenBoxRunner.ensure_running** → host/port/startup_mode/policy_path/extra_env 入参不变 → Windows 平台三个 env（JIUWENBOX_BUNDLED_PYTHON / JIUWENBOX_VENV_DIR / JIUWENBOX_RUNNER_PYTHON）同步。
- **改 shutdown 顺序** → step 1（DELETE 远端）必须在 step 2（停 box-server 子进程）之前 → enterprise 跳过 step 1 → 跑退出清理测试。
- **改 win32 `_is_std_cpython` 判定** → `python.exe` 父目录有 `python3*.dll` 且不在 Scripts/；**不要把 venv launcher 当标准 CPython**（jbx-sandbox 跑不了）→ 跑 Windows 平台探测测试。
- **加新 sandbox.type** → 在 gate 里显式区分（yuanrong 当前 skip）→ 同步 handlers/sandbox.py 的 action 分派。

## 与其它 spec 的关系

- [[S_01]] AgentWebSocketServer.start() 装配链里的 sandbox auto-start 步骤
- [[S_02]] handlers/sandbox.py 的 HANDLERS 注册（command.sandbox → handle_command_sandbox）
- [[S_03]] runtime/ 不碰 sandbox 启动（跨域门面约束）
