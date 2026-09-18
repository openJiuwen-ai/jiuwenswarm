# Redis ACL 用户名支持（支持连接非 default 用户）

- 日期：2026-09-18
- 里程碑 / commit：待提交
- 涉及模块：部署脚本 / 模板 / Gateway / Runtime / 文档
- 方案设计文档：[2026-09-redis-acl-username-design.md](2026-09-redis-acl-username-design.md)

## 背景与动机

外部 Redis 不允许使用 `default` 用户，但本项目连接 Redis 的用户名被硬编码：
Gateway 侧 `redis_client.py` 单机连接池写死 `username=None`（cluster 分支甚至没传
该参数），agent-runtime 侧**没有**用户名通路。不修改则无法接入禁用了 default
用户的外部 Redis。

新增 `REDIS_USERNAME`，支持经 K8s ConfigMap 注入到所有使用 Redis 的组件，
并可通过部署脚本 `.env.custom` 由用户指定。

## 方案（定案）

1. 用户名走独立 kwarg `username=`，与既有 `password=` 完全对称，不塞进连接串。
2. Gateway 经 gateway env ConfigMap、AgentServer 经 agentserver env ConfigMap、
   agent-runtime Pod 经 `runtime.template.yaml` 的 `env` 白名单，三处分别注入。
3. `REDIS_USERNAME` 非空时，内置 Redis 同步创建同名 ACL 用户并关闭 `default` 用户。
4. 内置 Redis 的密码经显式 `env` + `secretKeyRef` 注入，`args` 中写
   `>$(REDIS_PASSWORD)` 交由 k8s 展开，**不落进 Deployment manifest 明文**。
5. 设置了用户名但密码为空时直接 fail-fast，不渲染 `nopass`。

决策过程、被否方案与理由见配套设计文档。

## 实现

### jiuwenswarm（12 个文件）

**代码**

| 文件 | 改动 |
|---|---|
| `jiuwenswarm/extensions/redis/redis_client.py` | `RedisConfig` 新增 `username` 字段；`_normalize_password` 泛化为 `_normalize_optional_str` 供 username/password 共用；`from_mapping` 读取并归一化 `username`；`open()` 单机连接池的硬编码 `username=None` 改为 `self._cfg.username`；**cluster 分支补上此前完全缺失的 `username` 参数** |
| `jiuwenswarm/resources/config.yaml` | `redis:` 段新增 `username: ${REDIS_USERNAME:-}` |
| `jiuwenswarm/resources/secret_registry.yaml` | 新增 `redis.username: {medium: env, path: REDIS_USERNAME}` |
| `jiuwenswarm/resources/secret_registry.enterprise.yaml` | 同上 |

**部署脚本与模板**

| 文件 | 改动 |
|---|---|
| `deploy/enterprise/global_vars.sh` | 新增 `["REDIS_USERNAME"]=""`（挨着 `REDIS_PASSWORD`） |
| `deploy/enterprise/.env.example` | Redis 段新增 `REDIS_USERNAME=""` 及说明注释 |
| `deploy/enterprise/gen_env_custom.sh` | 生成的 `.env.custom` 中新增 Redis 配置段，含 `REDIS_HOST`/`REDIS_PORT`/`REDIS_USERNAME`/`REDIS_PASSWORD` 四个注释掉的键，供用户按需指定 |
| `deploy/enterprise/templates/gateway.template.env` | 新增 `REDIS_USERNAME=<<REDIS_USERNAME>>` |
| `deploy/enterprise/templates/agentserver.template.env` | 新增 `REDIS_USERNAME=<<REDIS_USERNAME>>` |
| `deploy/enterprise/templates/runtime.template.yaml` | agent-runtime Pod 的 `env` 新增 `REDIS_USERNAME` |
| `deploy/enterprise/redis_handler.sh` | 新增 `apply_redis_acl_if_needed()`（yq 注入 ACL args / env / 认证探针）与 `redis_deployment_has_acl()`；`render_redis_files()` 末尾调用前者 |
| `deploy/enterprise/check_handler.sh` | `ensure_redis_up()`：复用已有内置 Redis 时做 ACL 一致性校验并明确报错；新建内置 Redis 且启用 ACL 时提前创建 Secret |

### agent-runtime（3 个文件）

| 文件 | 改动 |
|---|---|
| `service/openjiuwen_runtime/service/config.py` | `ServiceConfig` 新增 `redis_username` 字段；`from_env` 读取 `REDIS_USERNAME`（空串归一为 `None`）；模块 docstring 的环境变量表补一行 |
| `service/openjiuwen_runtime/service/bootstrap.py` | `build_redis_client` 在 `cfg.redis_username` 非空时注入 `username` kwarg |
| `applications/agent_runtime/docs/spec/service-core.md` | 按模块 CLAUDE.md 的“spec 同步义务”补 username 通路说明 |

### 关键实现细节

**A. 内置 Redis 的 ACL 注入**（`redis_handler.sh`）

```yaml
env:
  - name: REDIS_USERNAME
    value: <用户名>
  - name: REDIS_PASSWORD
    valueFrom:
      secretKeyRef: {name: <SECRET_CM_NAME>, key: REDIS_PASSWORD}
args:
  - redis-server
  - --appendonly
  - "yes"
  - --user
  - <用户名>
  - on
  - '>$(REDIS_PASSWORD)'
  - ~*
  - +@all
  - --user
  - default
  - off
readinessProbe:
  exec:
    command: ["sh", "-c", 'redis-cli --user "$REDIS_USERNAME" -a "$REDIS_PASSWORD" --no-auth-warning ping']
```

- `args` 用 k8s 的 `$(VAR)` 展开，因此密码**必须**走显式 `env` 条目而非仅 `envFrom`
  （规避“变量需先前定义”的时序约束）。用户名非敏感，直接明文渲染。
- 探针改用 `sh -c` + `$VAR`，由容器内 shell 运行时解析，完全不依赖 k8s 展开时序。
- `REDIS_USERNAME` 为空时该函数直接返回，模板产物与改动前**逐字段一致**。

**B. 渲染顺序修正**（`check_handler.sh`）

`ensure_redis_up` 在 `check_gateway_up_dependency` 中执行，早于
`render_gateway_files`（Secret 的创建点）。启用 ACL 时提前调用
`render_secret_configmap` + `ensure_secret_configmap`（均幂等），
否则内置 Redis Pod 会卡在 `CreateContainerConfigError`，
且 `deploy_redis` 的 `wait_k8s_resource_ready` 会一直等待。

**C. 升级陷阱拦截**（`check_handler.sh`）

复用已有内置 Redis 时，若 `REDIS_USERNAME` 非空但该 Deployment 的 args 不含
`--user`，直接 `error` 退出并提示两条出路（改用外部 Redis / 清空用户名 /
`down redis` 重建并注意数据丢失），不静默降级。

## 验证

### 1. 语法与模式校验

```
bash -n redis_handler.sh check_handler.sh global_vars.sh gen_env_custom.sh \
        gateway_handler.sh runtime_handler.sh            → 全部 OK
python3 -m py_compile redis_client.py config.py bootstrap.py  → OK
yaml.safe_load: config.yaml / secret_registry{,.enterprise}.yaml → OK
```

### 2. `RedisConfig` 归一化（12 项，全部 PASS）

| 输入 | 期望 | 结果 |
|---|---|---|
| 无 `username` 键（历史配置） | `None` | PASS |
| `username: ""` | `None` | PASS |
| `username: "   "` | `None` | PASS |
| `username: false`（YAML 解析） | `None` | PASS |
| `username: null` | `None` | PASS |
| `username: "svc_acct"` | `"svc_acct"` | PASS |
| `username: 12345` | `"12345"` | PASS |
| cluster 模式 + username | `"svc_acct"` | PASS |
| `password: ""` | `None`（泛化改动未破坏原语义） | PASS |
| `password: "s3cret"` | `"s3cret"` | PASS |
| username + password 共存 | 各自独立 | PASS |
| `key_prefix` 行为 | 不受影响 | PASS |

### 3. 连接点参数实测（stub 掉 `redis.asyncio`，捕获构造 kwargs）

```
单机 ConnectionPool:  host='redis.internal' port=6379 username='svc_acct' password='pw' db=0
cluster RedisCluster: username='svc_acct' password='pw'
未配置 username 时:   username=None                ← 与改动前一致
```

证明两处连接站点的参数已正确传递，且 cluster 分支此前缺失的 username 已补上。

### 4. agent-runtime `bootstrap.build_redis_client`（4 组，全部 PASS）

monkeypatch `redis.asyncio.from_url` 捕获 kwargs：

| 环境变量 | `cfg.redis_username` | `from_url` 收到的 kwargs |
|---|---|---|
| 均未设置 | `None` | 不含 `username` 键（回归基线） |
| `REDIS_USERNAME=svc_acct` + `REDIS_PASSWORD=pw` | `'svc_acct'` | `username='svc_acct' password='pw'` |
| `REDIS_USERNAME=""` | `None` | 不含 `username` 键 |
| `REDIS_USERNAME="   "` | `None` | 不含 `username` 键 |

### 5. 内置 Redis ACL 渲染（真实模板 + 真实 `redis_handler.sh`）

用真实 `templates/redis.template.yaml` 渲染后调用 `apply_redis_acl_if_needed`：

- **未配置用户名**：`args` 为原三条、`env: null`、探针 `["redis-cli","ping"]`
  ——与改动前逐字段一致。
- **配置用户名 + 密码**：`args` 末尾追加
  `--user acluser on '>$(REDIS_PASSWORD)' ~* +@all --user default off`；
  `env` 为 `REDIS_USERNAME` + `REDIS_PASSWORD(secretKeyRef)`；
  探针变为 `sh -c 'redis-cli --user "$REDIS_USERNAME" ...'`。
- **k8s 结构校验**：占位符全部替换后 `kubectl apply --dry-run=client -f` →
  `service/… created (dry run)` ×2 + `deployment.apps/… created (dry run)`，无残留占位符。

### 6. 回归测试

```
cd agent-runtime/service && uv run pytest -q
  → 226 passed, 5 skipped

cd agent-runtime/applications/agent_runtime && uv run pytest -q
  → 525 passed, 9 skipped, 76.06s
```

（`deploy/enterprise` 为 bash 脚本，仓库内无 shell 测试框架；jiuwenswarm
Python 包的 pytest 环境未安装依赖，`RedisConfig` 相关逻辑改用上面的等价
独立用例覆盖。）

### 7. 交叉核对

- `check_handler.sh` 仅被 `deploy.sh` 引用，而 `deploy.sh` 同时 source 了
  `redis_handler.sh`(L18) 与 `configmap_secret_handler.sh`(L22)，
  新增的三个跨文件函数调用在运行期均可解析。
- AgentServer Pod 若 `envFrom` 引用 gateway env ConfigMap 会额外注入 ~50 个键；
  逐键核对确认 agent-runtime 服务端代码只读取其中 `NAMESPACE`（`main.py:566`），
  且取值与本命名空间一致。最终方案未采用该注入方式，此处仅作被否方案的论证依据。

## 兼容性与影响面

| 项 | 结论 |
|---|---|
| 默认行为 | `REDIS_USERNAME` 默认空 → 代码与模板都退化为原行为，**逐字段一致** |
| 存量部署 | 内置 Redis Deployment 已存在则不重渲染，不受影响；若此时新配用户名会被升级拦截报错挡住 |
| 内置 Redis 重建 | emptyDir 存储，重建会清空 gateway session map / cron 数据（报错信息已提示） |
| Secret 创建时机 | 启用 ACL 时提前到依赖检查阶段；内容与原先一致 |
| 外部 Redis | 仅新增可选路径，`REDIS_HOST`/`REDIS_PASSWORD`/`REDIS_MODE` 等既有通路不变 |
| 配置面 | `.env.custom` 新增可选键，旧配置文件无需修改 |

## 补充（第二轮，2026-09-18）：集群模式与真环境验证

第一轮只覆盖单机 Redis。为接入一个真实的 6 节点 ACL 集群做端到端验证时，
`REDIS_MODE=cluster` 这条路径依次暴露出三个缺陷，另有一个发布流程问题导致部署被阻塞。

### 缺陷 1：cluster 模式下"集群协议打单实例"的静默降级

- **位置**：`deploy/enterprise/check_handler.sh` → `ensure_redis_up()`
- **现象**：内置 Redis 恒为单实例（`redis.template.yaml` 无 `--cluster-enabled`，
  `replicas: 1`）。但 `REDIS_MODE=cluster` 而 `REDIS_HOST` 为空时，脚本仍会去
  部署/复用内置 Redis，渲染出必然失败的配置，且要到运行期才暴露。
- **修复**：在 `REDIS_CHECKED` 幂等标记之后加 guard —— cluster 模式**必须**显式给出
  外部集群地址，否则 `error` 退出，并提示正确写法与"改回 standalone"这条出路。
  不静默降级。

### 缺陷 2：多节点 cluster URL 拼接错误

- **位置**：`deploy/enterprise/runtime_handler.sh` → `gen_runtime_file()`
- **现象**：原实现 `redis+cluster://${redis_host}:${redis_port}` 在单节点时正确，
  多节点时整串被塞进 host 位、末尾再补一个端口，拼出
  `redis+cluster://h1:6379,h2:6379:6379` 这种坏 URL。
- **修复**：按逗号拆分 `REDIS_HOST`，逐项归一化 —— 已带端口的项不再追加端口、
  去空白、容忍尾随逗号 —— 再重新拼接；`REDIS_HOST` 为空时 `error` 退出。
  cluster 无库号，URL 不带 `/<db>`。

### 缺陷 3：`RedisCluster.from_url` 无法解析多节点 URL（仅真环境暴露）

- **位置**：`agent-runtime` → `service/openjiuwen_runtime/service/bootstrap.py`
- **现象**：修复缺陷 2 后 URL 拼接已正确，但 redis-py 的 `RedisCluster.from_url()`
  走 `urllib.parse`，把 netloc 里**首个 `:` 之后整体**当作 port：

  ```
  ValueError: Port could not be cast to integer value as
              '6379,10.42.0.204:6379,10.42.0.201:6379,...'
  ```

  `from_url` 天生只接受单节点，逗号列表无法经由它构造。这是纯静态检查与单测
  都覆盖不到的一类问题 —— 前一轮的 stub 验证恰好绕过了它。
- **修复**：新增 `_parse_cluster_nodes()` 拆解节点列表；`_build_redis_cluster_client`
  分两条路径 ——

  | 输入 | 路径 |
  |---|---|
  | 单节点 | 仍走 `from_url`，query / ssl / 凭据的解析语义完全不变 |
  | 多节点 | 构造 `ClusterNode` 列表显式传给 `startup_nodes`；另用首节点还原成单节点 URL 喂给 redis 自己的 `parse_url` 取 query / ssl 等连接参数，保证两条路径语义一致 |

  集群拓扑由 `CLUSTER SLOTS` 自发现，种子节点本不必给全；这里给全量列表，
  任一首节点先可达即可建连。原有 `db` 快速失败校验保持不变。

### 问题 4：发布包未同步（非代码缺陷，但阻塞部署）

实际部署用的是发布包 `JiuwenClaw_depolyTool_024/`，**不是** `sources/jiuwenswarm`。
第一轮改动全部落在源码仓库，发布包的模板里**完全没有 username 概念**，直接导致
网关以 `AUTH <password>` 打到已 `off` 的 default 用户，报
`invalid username-password pair or user is disabled`。

发布包需同步 5 处：

| 文件 | 改动 |
|---|---|
| `templates/gateway-config.template.yaml` | `redis:` 段新增 `username: ${REDIS_USERNAME:-}` |
| `templates/gateway.template.env` | 新增 `REDIS_USERNAME=<<REDIS_USERNAME>>` |
| `templates/runtime.template.yaml` | agent-runtime Pod 的 `env` 新增 `REDIS_USERNAME` |
| `global_vars.sh` | 新增 `["REDIS_USERNAME"]=""` |
| `runtime_handler.sh` | 同缺陷 2 的多节点 URL 修复 |

发布包是扁平结构（脚本直接在根目录），与源码仓库的 `deploy/enterprise/` 并非
同一布局；且该包的 `templates/gateway-config.template.yaml` 与源码仓库的
`jiuwenswarm/resources/config.yaml` 已经分叉（前者多 `operation_timeout` /
`health_check_interval` 两项），**不能整目录覆盖**，只能逐项同步。

### 验证（真环境）

在一台单节点 K3s 上，用独立脚本 `~/redis-cluster-test/deploy-redis-cluster.sh`
在 `default` 命名空间部署了一套 **3 主 3 从** ACL Redis Cluster（`testuser`，
`default` 用户 `off`），与部署脚本完全解耦。

**集群侧**

| 项 | 结果 |
|---|---|
| `cluster_state` | `ok`；`cluster_known_nodes=6`；`cluster_size=3` |
| 匿名访问 | `NOAUTH Authentication required.` |
| 只给密码不给用户名 | `WRONGPASS` —— 证明 `default` 用户确已关闭 |
| ACL 认证 + 跨槽位 SET/GET | 正常（跟随 MOVED 重定向） |
| 主从复制 | `connected_slaves:1 state=online` |

**跨命名空间**：从 `rapheal-entp` 的 Pod 经无头 Service DNS
（`redis-cluster-headless.default.svc.cluster.local`）与 Pod IP 均可连通，
ACL 认证下读写正常。集群内**零 NetworkPolicy**，namespace 是命名/RBAC 边界
而非网络边界。

**URL 构造与解析**：在与 agent-runtime 同镜像、同挂载的 Pod 内跑**真实那份代码**。

- 节点列表解析 6 组输入全部通过（含缺端口、带空格、尾随逗号），
  非数字端口与空列表按预期报错；
- `db` 快速失败在 `/1` 与 `?db=2` 两种写法下仍生效；
- 对真实 6 节点集群：

  ```
  种子节点 6 个；PING -> True；cluster_state=ok；SET/GET -> b'hello-cluster'
  ```

### 已知风险

测试集群的节点地址以 **Pod IP** 上报，且部署时用了
`--cluster-announce-ip $(POD_IP)`：Pod 一旦重建，新 IP 与集群拓扑中记录的旧 IP
不一致，`cluster_state` 会退化为 `fail`，且 `nodes.conf` 持久化在 PVC 上，
重启无法自愈。生产环境应改用 `--cluster-preferred-endpoint-type hostname`
（Redis 7+）或稳定的 Service DNS，并确认客户端能解析主机名端点 ——
本轮未对此做验证。

## 开放问题与后续计划

1. ~~未做真环境验证（最重要）~~ **部分完成**（第二轮）。已覆盖：真实 ACL **外部**
   集群的连接与读写、跨命名空间访问、`RedisCluster` 多节点构造，详见上文
   "补充（第二轮）"。
2. **内置 Redis 启用 ACL 的路径仍未验证**。`redis_handler.sh` 注入的 `args` 中
   `$(REDIS_PASSWORD)` 依赖 k8s "变量需在容器 env 中先前定义"的展开语义，
   本轮未在真实集群部署过启用 ACL 的内置 Redis。建议发版前补一次全新 K8s 部署验证。
3. **内置 Redis ACL 权限为 `~* +@all`**，未按最小权限收敛。
4. 未覆盖 `rediss://`（TLS）与 Sentinel 拓扑。
5. 跨命名空间共享同一内置 Redis 的场景未考虑，
   `redis_deployment_has_acl` 与 `ensure_redis_up` 同口径按本命名空间判定。

## 提交说明

改动跨两个独立 git 仓库，需**分开提交**：

- `jiuwenswarm`：本文档 + 设计文档 + 15 个文件
  - 第一轮 12 个：代码 4 + 部署脚本与模板 8
  - 第二轮 3 个：`check_handler.sh`（cluster guard）、
    `runtime_handler.sh`（多节点 URL）、`.env.example`（cluster 模式说明）
- `agent-runtime`（嵌套独立仓库）：4 个文件，并按其 `docs/feature/` 约定
  另建一份记录
  - 第一轮 3 个：`config.py` / `bootstrap.py` / `docs/spec/service-core.md`
  - 第二轮 1 个：`bootstrap.py`（`_parse_cluster_nodes` + 多节点
    `startup_nodes` 构造）

> **另有发布包 `JiuwenClaw_depolyTool_024/` 不在任何 git 仓库内**，需单独同步
> 上文"问题 4"表中的 5 处改动，否则源码仓库改完部署仍会自动失败。

## 变更记录

| 日期 | 轮次 | 内容 |
|---|---|---|
| 2026-09-18 | 第一轮 | `REDIS_USERNAME` 通路打通（单机）；静态检查 + 单测 + stub 级验证 |
| 2026-09-18 | 第二轮 | cluster 模式三个缺陷修复 + 发布包同步；真实 6 节点 ACL 集群端到端验证 |
