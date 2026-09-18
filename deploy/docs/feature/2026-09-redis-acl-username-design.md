# Redis ACL 用户名支持 —— 方案设计

- 日期：2026-09-18
- 里程碑 / commit：待提交
- 涉及模块：部署脚本 / 模板 / Gateway / Runtime / 文档
- 配套实施记录：[2026-09-redis-acl-username.md](2026-09-redis-acl-username.md)

## 背景与动机

外部 Redis 不允许使用 `default` 用户（ACL 剥离 default 权限是企业侧常见安全要求），
而本项目连接 Redis 时用户名被硬编码为 `None`，即固定走 default 用户：

- `jiuwenswarm/extensions/redis/redis_client.py` 单机连接池 `username=None` 硬编码；
  cluster 分支构造 `RedisCluster` 时**根本没传** `username`。
- agent-runtime 侧连接串 `OPENJIUWEN_SERVICE_REDIS_URL` 不含凭据，密码另经
  `REDIS_PASSWORD` 环境变量以 kwarg 注入，用户名无对应通路。

不改的后果：无法接入禁用了 default 用户的外部 Redis，只能退回自建 Redis，
与“必须用外部 Redis”的部署要求冲突。

## 方案（定案）

### 决策 1：用户名走独立 kwarg，不塞进连接串

`REDIS_USERNAME` 作为独立环境变量，在客户端构造时作为 `username` kwarg 注入，
与既有 `REDIS_PASSWORD` 完全对称。

**被否方案**：把用户名写进 `OPENJIUWEN_SERVICE_REDIS_URL`
（`redis://user:pass@host:port/db`）。否决理由：用户名需 percent-encode 才安全，
且会把凭据推进明文 URL——与当初引入 `REDIS_PASSWORD` kwarg 的动机（2026-08-28）相悖。

### 决策 2：AgentServer 侧经 `agentserver.template.env` 注入（需求方确认）

初次方案误将 AgentServer 的注入点放在 gateway env ConfigMap。经需求方指出并核实：
AgentServer 走 `agentserver.template.env` → `render_agentserver_env_configmap` →
ConfigMap `jiuwenclaw-agentserver-env` → `agentserver.template.json` 的
`envFrom.configMapRef`，与 gateway 的 ConfigMap 无关。

**被否方案 A**：复用 gateway env ConfigMap 并让 AgentServer `envFrom` 引用它。
否决理由：`envFrom` 是全量注入，会把 gateway 的 ~50 个键（`ROLE=gateway`、
`EXTENSION_DIRS=.../gateway/extensions`、`GATEWAY_DB_*` 等）灌进 AgentServer Pod。
实测确认服务端代码只读其中 `NAMESPACE` 一个键且取值相同（见“验证”），风险可控，
但语义仍然错位，不如按组件各自的 env 模板注入。

**被否方案 B**：新建独立 ConfigMap。否决理由：为一个非敏感键新增一套资源、
handler 与两处 `envFrom` 改动，收益不足。

### 决策 3：agent-runtime Pod 另需独立注入

`runtime.template.yaml` 里的 agent-runtime Pod 才是真正使用
`OPENJIUWEN_SERVICE_REDIS_URL` 的组件（checkpointer），它不读 agentserver env ConfigMap。
因此该 Pod 的 `env` 白名单中也必须显式加 `REDIS_USERNAME`——仅改
`agentserver.template.env` 无法覆盖它。

### 决策 4：自建 Redis 同步启用 ACL（需求方确认）

由 `REDIS_USERNAME` 是否非空驱动：非空即为内置 Redis 创建同名 ACL 用户并关闭
`default` 用户；为空则模板保持原样。

**被否方案**：只支持外部 Redis、自建 Redis 维持无认证。否决理由：需求方要求内置
Redis 也建 ACL 用户。

### 决策 5：密码经 Secret 展开，不落进 manifest 明文（需求方选定 D1）

内置 Redis 容器用显式 `env` + `valueFrom.secretKeyRef` 注入 `REDIS_PASSWORD`，
`args` 里写 `>$(REDIS_PASSWORD)`，由 k8s 在启动时展开。

**被否方案 D2**：直接把 `<<REDIS_PASSWORD>>` 渲染进 `args`。否决理由：密码会明文
出现在 Deployment manifest 中，任何能读 pod spec 的人都能看到。

### 决策 6：用户名有密码为空 → 直接 fail-fast，不渲染 `nopass`

原方案计划在空密码时把 ACL 修饰符渲染成 `nopass`。实现时否决：

- ACL 用户不设密码本身是反模式；
- 空密码会被 `configmap_secret_handler.sh` 跳过，Secret 里根本不存在
  `REDIS_PASSWORD` 这个 key，`secretKeyRef` 会让 Pod 卡在
  `CreateContainerConfigError`，而 `deploy_redis` 的 `wait_k8s_resource_ready`
  会一直等待，表现为部署静默挂死。

改为在渲染期直接 `error` 报错并给出两条出路。

### 决策 7：cluster 模式必须显式给外部集群，不静默降级（第二轮）

`REDIS_MODE` 是**消费方的协议选择器**，不是**供应方的拓扑选择器**：内置 Redis
恒为单实例（`redis.template.yaml` 无 `--cluster-enabled`，`replicas: 1`）。
因此 `REDIS_MODE=cluster` 而 `REDIS_HOST` 为空属于配置错误，而非"由脚本自动装一个
集群出来"。

**被否方案**：自动部署一个集群模式的内置 Redis。否决理由：单节点 K3s 上跑集群
只能靠多 Pod 模拟，与内置 Redis 的 `replicas: 1` + emptyDir 定位冲突，
且会把 `redis_handler.sh` 的 ACL 注入逻辑复杂化。

改为在 `ensure_redis_up()` 中 fail-fast 报错，并给出"填外部集群地址"与
"改回 standalone"两条出路。

### 决策 8：多节点 cluster URL 的解析职责放在 agent-runtime 侧（第二轮）

`redis+cluster://h1:6379,h2:6379` 这一形式**无法**经由 redis-py 的
`RedisCluster.from_url()` 构造 —— 它走 `urllib.parse`，把 netloc 里首个 `:`
之后整体当 port。可选方案有二：

- **A（选定）**：agent-runtime 侧自行拆解列表，多节点时显式传 `startup_nodes`。
- **B（否决）**：给每个节点配一个独立的稳定 DNS 名，把 URL 退化成单节点。
  否决理由：要求用户在 `.env.custom` 里额外维护 DNS 名，且掩盖了"客户端应支持
  多节点"这一事实；集群拓扑本就由 `CLUSTER SLOTS` 自发现，种子节点给多个是为了
  可用性冗余，正是 `startup_nodes` 的设计用途。

方案 A 保留单节点路径走 `from_url` 不变，避免影响既有单节点部署的
query / ssl / 凭据语义。

## 实现

### 配置与代码通路

| 组件 | 注入点 | 消费点 |
|---|---|---|
| Gateway | `gateway.template.env` → gateway env ConfigMap | `config.yaml` 的 `redis.username: ${REDIS_USERNAME:-}` → `RedisConfig.from_mapping` |
| AgentServer（claw 角色） | `agentserver.template.env` → agentserver env ConfigMap | 同上（config.yaml 由 configMap 挂载） |
| agent-runtime Pod | `runtime.template.yaml` 的 `env` | `ServiceConfig.from_env` → `bootstrap.build_redis_client` 的 `username` kwarg |

### 空值语义

两个 env ConfigMap 的渲染函数都带 `awk -F'=' '$2 != ""'`
（`gateway_handler.sh:18`、`runtime_handler.sh:13`），空值行**不会**写进 ConfigMap，
envar 直接不存在。代码侧再统一把空串归一为 `None`，两道保险叠加，
未配置时行为与改动前逐字段一致。

### 内置 Redis ACL 的渲染顺序修正

`ensure_redis_up` 位于 `check_gateway_up_dependency`，跑在 `render_gateway_files`
**之前**；而 Secret 是在 `render_gateway_files` 里才创建的。内置 Redis 一旦引用
Secret，Pod 会卡在 `CreateContainerConfigError`。修正为：启用 ACL 时在 `deploy_redis`
前先调 `render_secret_configmap` + `ensure_secret_configmap`（两者幂等）。

### 升级陷阱拦截

存量部署的内置 Redis 无 ACL 用户，且 `ensure_redis_up` 对已存在的 Deployment
直接复用不重建。此时新配 `REDIS_USERNAME` 会让 gateway / agentserver 拿一个
不存在的用户去认证而全量失败。修正为：复用分支用 `redis_deployment_has_acl`
（查 Deployment args 是否含 `--user`）检测，不匹配则明确报错，不静默降级。

## 验证

见配套实施记录 [2026-09-redis-acl-username.md](2026-09-redis-acl-username.md)
的“验证”及“补充（第二轮）”两节（含逐项命令与实测结果）。

关键结论：

- `REDIS_USERNAME` 未配置时，Gateway 的 `ConnectionPool` 收到 `username=None`、
  agent-runtime 的 `from_url` 收到的 kwargs 中**不含** `username` 键，
  与改动前完全一致。
- 第二轮已在真实 3 主 3 从 ACL 集群上跑通端到端连接与读写，
  并确认 `default` 用户确已关闭（匿名 `NOAUTH`、仅密码 `WRONGPASS`）。

## 兼容性与影响面

- **默认行为不变**：`REDIS_USERNAME` 默认空，代码与模板两条路径都退化为原行为。
- **仅影响新部署的内置 Redis**：已存在的内置 Redis Deployment 不重渲染；
  此时若新配用户名会被“升级陷阱拦截”报错挡住，而非静默失败。
- **重建内置 Redis 会丢数据**：内置 Redis 为 emptyDir，`down redis` 会清空
  gateway 的 session map / cron 数据。报错信息中已提示。
- **Secret 提前创建**：启用 ACL 时 Secret 会在 gateway 模块的依赖检查阶段
  而非渲染阶段创建。此时 `DEPLOY_VARS` 中的各 `*_DB_PASSWORD` 已就绪，
  内容与原先一致；仅创建时机提前。
- **不影响**已有 `REDIS_PASSWORD` / `REDIS_MODE` / cluster 等既有通路。

## 开放问题与后续计划

1. ~~未做真环境验证~~ **部分完成**（第二轮）。已在真实 3 主 3 从 ACL **外部集群**
   上跑通连接、跨槽位读写与跨命名空间访问，并确认 `default` 用户确已关闭
   （匿名 `NOAUTH`、仅密码 `WRONGPASS`）。
   **仍未验证**的是**内置 Redis 启用 ACL** 这条路径：`redis_handler.sh` 注入的
   `args` 中 `$(REDIS_PASSWORD)` 依赖“变量需在容器 env 中先前定义”这一 k8s 展开
   语义，本轮未在真实集群部署过启用 ACL 的内置 Redis。
   建议发版前补一次全新 K8s 部署验证。
2. **未覆盖 Sentinel / TLS**：外部 Redis 若走 `rediss://` 或哨兵，本次未涉及，
   用户名通路本身与协议无关，但未实测。
3. **内置 Redis 的 ACL 权限为 `~* +@all`**，未按最小权限收敛。如需收紧需另行设计。
4. **多命名空间**：`redis_deployment_has_acl` 按 `REDIS_NAME` + namespace 查询，
   与既有 `ensure_redis_up` 的判定口径一致；跨命名空间共享 Redis 的场景未考虑。
