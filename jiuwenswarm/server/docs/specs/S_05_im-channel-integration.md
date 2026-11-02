# IM 频道接入

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_05 / im-channel-integration |
| 关联模块 | jiuwenswarm/server/im/{im_connector/, im_hosting/} |
| 最近一次修订日期 | 2026-10-15 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本规约管什么

- im_connector Plugin 协议（`plugin.py`）：每个连接器必须实现的接口方法集。
- im_connector Registry（`registry.py`）：连接器注册与按 name 查找。
- 三个内置连接器：dingtalk / feishu / welink 的 `cli.py + parser.py + plugin.py` 分工。
- im_hosting 托管服务（`im_hosting/service.py`）：`poll_due()` / `run_poll_once()` 按策略轮询 IM target。
- im_hosting 子域：poller（轮询器）/ auto_host（自动托管）/ store（状态存储）/ gate（门禁）/ policy（策略）/ login（登录）/ reply_bridge（回复桥接 IM 回复 → AgentServer 入站）/ connectors（连接器选择）/ cli_resolve（CLI 解析）/ paths（路径）。
- AgentWebSocketServer.start() 末尾非 enterprise 下的 `im_hosting.start()`（企业版由 sidecar 管理）。

### 本规约**不**管什么

| 子主题 | 归属 spec |
|---|---|
| HANDLERS 注册表 / 默认路径 fallback | [[S_02]] request-pipeline-dispatch |
| AgentWarmPool / agent_adapter | [[S_03]] runtime-agent-management |
| sandbox auto-start 条件链 | [[S_04]] sandbox-lifecycle |

## 不变量

1. **Plugin 协议是连接器唯一入口**：所有 IM 连接器必须实现 `im_connector.plugin.Plugin` 协议接口，注册到 `registry.py`；新增频道（如 xiaoyi / weixin）先实现 Plugin，再注册。
2. **cli.py 是连接器专属 CLI**：每个连接器（dingtalk / feishu / welink）的 `cli.py` 提供频道专属命令行工具（login / poll / reply），不共享通用 CLI。
3. **parser.py 是连接器专属消息解析**：IM 原始消息 → AgentServer 入站消息的转换逻辑封装在每个连接器的 `parser.py`，不跨连接器复用。
4. **im_hosting 独立于 AgentWebSocketServer 生命周期**：`AgentWebSocketServer.start()` 里用 `get_hosting_service().set_agent_manager(TenantAgentPool.get_instance()).start()` 启动，异常被 catch（不阻断 WS 启动）。
5. **企业版跳过本地 im_hosting**：enterprise 下 AgentWebSocketServer.start() 里 `im_hosting.start()` 调用被 `if not is_enterprise():` 门控跳过——IM 托管由 K8s sidecar 管理。
6. **reply_bridge 双向桥接**：IM 回复 → reply_bridge → AgentServer 入站消息（走 HANDLERS 同一条分发链）；AgentServer send_push → gateway_push → IM 回复。
7. **poller 按 policy 轮询**：`service.poll_due()` 按 `im_hosting/policy.py` 定义的间隔/窗口决定是否轮询，不硬编码。
8. **store 状态持久化**：im_hosting 状态（connector 登录态 / poll 游标 / 消息 ack）落本地磁盘，进程重启不丢失。

## 接口契约

### im_connector.plugin.Plugin（协议）

```python
class Plugin:
    # 连接器标识（registry 按 name 查找）
    name: str  # 例: "dingtalk" / "feishu" / "welink"
    # 连接配置结构（由 config.yaml channels.<name>.<connector> 注入）
    config_cls: type[PydanticBaseModel]

    async def connect(self, config) -> None: ...       # 建立 IM 连接（WS / OAuth / CLI）
    async def disconnect(self) -> None: ...
    async def poll_once(self) -> list[IMMessage]: ...   # 拉取一次新消息
    async def send_reply(self, target: str, content: str) -> None: ...
    async def login_via_cli(self) -> None: ...          # 交互式登录（CLI 工具）
```

### im_connector.registry

```python
# 进程启动时一次性 import 所有 connectors → registry.register(connector_instance)
registry.register(plugin: Plugin)
registry.get(name: str) -> Plugin | None
registry.list() -> list[Plugin]
```

### im_hosting.service.HostingService

```python
class HostingService:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def poll_due(self) -> None: ...     # 按 policy 决定是否 poll
    async def run_poll_once(self) -> None: ...
    def set_agent_manager(self, manager: AgentManager) -> None: ...  # AgentServer 启动时注入
```

## 执行路径 / 装配关系

### P1：AgentServer 启动时自动托管 IM

```
AgentWebSocketServer.start(listen=True)
  → enterprise？→ skip
  → get_hosting_service()（单例）
  → hosting.set_agent_manager(TenantAgentPool.get_instance())
  → hosting.start()（内部启动 poller 周期轮询）
     → policy.poll_interval 间隔
     → poller.poll_once()
        → im_connector.registry.get(name).poll_once()
        → parser.py 把 IM 原始消息 → AgentServer AgentRequest
        → 送进 pipeline.dispatch_parsed_request（走 HANDLERS 同一条分发链）
     → reply_bridge.reply()
        → gateway_push.build_server_push_wire → IM connector.send_reply()
```

### P2：CLI 显式托管

```
jiuwenswarm-im-hosting start --connector dingtalk --login
  → im_hosting auto_host 策略 → 登录 → register to store
  → 进入周期轮询同 P1 后半段
```

### P3：handlers/im_hosting.py（HANDLERS 注册路径）

```
dispatch.HANDLERS[ReqMethod.IM_HOSTING_STATUS]
  → handlers/im_hosting.handle_im_hosting(ctx)
    → 读 store 状态 → sink.send_wire 返回托管状态 JSON
```

## 修改检查单

- **新增 IM 连接器** → 实现 `im_connector.plugin.Plugin` 协议（name + config_cls + connect/disconnect/poll_once/send_reply/login_via_cli）→ 放在 `im_connector/connectors/<name>/` 下（plugin.py + cli.py + parser.py 三件套）→ registry 注册 → AgentServer.start() 流程里 policy.poll_interval 确认能 cover 新 connector。
- **改 Plugin 协议方法签名** → 所有三个现有连接器同步改 → registry 不崩 → CLI 登录不崩。
- **改 im_hosting.policy 轮询间隔** → 同步 poller.poll_due() 计算 → service.start() 里周期 timer 重建。
- **改 enterprise gate** → `if not is_enterprise(): hosting.start()` 门控确认还在 → 企业 sidecar 管理的托管不被本地重复启动。
- **改 store 持久化路径 / schema** → store.py 同步 → 老版本 store 兼容（不阻塞启动）→ 跑进程重启 poll 游标不丢测试。
- **改 reply_bridge 双向桥接** → 入站 parser 与出站 send_reply 的转换逻辑同步 → 不打破 P2（回复桥接）路径。

## 与其它 spec 的关系

- [[S_01]] AgentWebSocketServer.start() 装配链里的 im_hosting.start() 步骤
- [[S_02]] handlers/im_hosting.py 的 HANDLERS 注册（IM_HOSTING_STATUS 等）+ reply_bridge 送进 pipeline 走同一条分发链
