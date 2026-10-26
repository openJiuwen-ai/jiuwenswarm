# 内部链路双向证书认证：使用指南与设计说明

本文说明 Gateway、Runtime、AgentServer 之间的 HTTP/SSE 链路认证，以及管理服务接入方式。先介绍部署与使用，再说明实现原理、数据保存方式和安全边界。

## 一、如何使用

### 1. 默认关闭，显式开启

功能默认关闭。未配置该变量，或显式配置为 `off`，均不启用链路证书认证：

```ini
JIUWENSWARM_LINK_MTLS_MODE=off
```

需要启用时，在部署工具现有的 `.env.custom` 中显式设置：

```ini
JIUWENSWARM_LINK_MTLS_MODE=enforce
```

只保留一条同名设置，避免重复赋值造成误解。值为枚举，不使用 `true`、`false` 或 `on`。

| 值 | 行为 | 是否启用认证 |
| --- | --- | --- |
| `off`（默认） | 保持原有传输路径，不执行证书生成和材料持久化流程 | 否 |
| `observe` | 预检本地证书材料；不强制依赖证书、不切换 HTTPS、不实施远端角色认证 | 否 |
| `enforce` | 部署工具准备证书与绑定材料；受管内部链路启用 HTTPS、双向证书和绑定校验；校验失败不退回明文 | 是 |

“没有设置开关，但目录或数据库里有证书”不会自动启用本功能。反过来，修改 `.env.custom` 也不会立即改变已经运行的进程，需要通过部署流程使设置生效。

### 2. 部署前提

- 使用配套发布的 JiuwenSwarm、Runtime 及基础库。Gateway、Runtime、AgentServer 镜像需要包含本功能；仅修改旧镜像的环境变量不能安装新功能。
- 源码安装和 CI 必须安装 `pyproject.toml` 声明的 Runtime foundation 依赖，不能只安装 agent-core。跨仓库发布时先合入 Runtime 的配套实现，再构建 JiuwenSwarm；联合验证未合入代码时，应显式安装所验证 Runtime 源码中的 `foundation[link-mtls]`，不要用测试替身代替证书实现。
- 使用企业部署工具已有的 MySQL 或 PostgreSQL 配置。Gateway 与 Runtime 使用不同数据库；PostgreSQL 也可使用分离的 schema。不得共用同一个数据命名空间。
- Linux 部署节点需要具备原部署工具依赖（包括 Bash、jq、yq、kubectl），以及本地 Docker daemon。容器中的配套 Runtime 基础库负责证书与数据库操作，不要求为本功能在部署主机安装 Python 虚拟环境。
- 部署节点可访问数据库及受管服务；kubectl 具有目标命名空间相应资源的读写权限。使用 headless 数据库 Service 时，需能确定唯一可写目标，否则应配置明确的数据库主节点地址。
- 保持部署节点及各服务节点时间同步。检查服务名称、命名空间、集群 DNS 域及端口，避免安装后无计划地改变证书对应地址。

本功能不要求增加证书管理 Pod，也不要求指定版本之外的主机持久证书目录。

### 3. 无内置 Manager 的部署

以下命令在源码的 `deploy/enterprise` 目录或配套部署包目录执行。命名空间 `example-app` 仅作示例，应替换为实际目标。

已有数据库、存储、镜像和业务配置保持原有配置方式。使用部署工具下发初始业务配置的交付形态：

```ini
APPLY_PATCH=true
JIUWENSWARM_LINK_MTLS_MODE=enforce
```

```bash
# 源码部署时先同步当前源码配置模板。
./update_conf.sh

# 可先渲染检查清单；此步骤不会签发证书或向数据库写入材料。
./deploy.sh up gateway web runtime -n example-app --render-only

# 正式应用清单，并准备、持久化或复用证书。
./deploy.sh up gateway web runtime -n example-app
```

该流程不启动 Manager Server、Manager Web 或 Identity Center。配置下发由部署工具使用 `manager` 角色证书完成；`manager` 是协议中的服务角色，不要求运行仓库内置的 Manager 应用。

`.env.custom` 中原有的模型、数据库和存储参数仍须正确配置。链路证书认证不能代替业务参数校验，也不会把空模型配置自动补成有效模型。

源码挂载仍使用已有的 `MODE=dev`、源码路径及节点设置；正式镜像部署使用 `MODE=product`。这些是代码加载方式，不是证书认证开关。生产镜像应打包配套 Runtime foundation 及所需 HTTP/TLS 依赖，不能用源码验证结果推断任意旧镜像均可开启本功能。

### 4. 可选的内置管理链路

如明确使用仓库提供的 Manager/Identity 参考实现，应显式部署 `manager` 模块。例如：

```ini
APPLY_PATCH=false
LOGIN_AUTH_SIMULATE=false
JIUWENSWARM_LINK_MTLS_MODE=enforce
```

```bash
./deploy.sh up gateway web manager runtime -n example-app
```

`APPLY_PATCH=false` 表示不由部署工具执行上述业务补丁下发，不会自动把 `manager` 加入模块列表。本节是可选参考形态，不属于无 Manager 交付的必需步骤。

用户登录、用户与组织权限、企业版入口等是独立配置。本功能认证服务身份，不认证浏览器用户，也不替代业务准入检查。

Manager 原有的多实例模型保持不变：`instance_info.jiuwenclaw_id` 仍由 Manager 按原逻辑为每个实例生成，不能改成 Manager 证书中的固定值。Manager 证书只证明调用方属于 `manager` 服务角色；每个 `jiuwenclaw_id` 再通过 `instance_link_binding` 选择自己的绑定编号、代次、Gateway/Runtime 身份和目标证书指纹。

同一部署工具同时安装的一组 Manager、Gateway 和 Runtime，会在第一次配置同步时自动采用与两个配置地址完全匹配的部署绑定，但不会改写 Manager 已生成的实例 ID。其他实例必须先登记各自的链路绑定；显式登记的两个 mTLS 地址也必须与该实例在 `instance_info` 中的 Gateway/Runtime 配置地址一致。同一 Gateway 或 Runtime 不能同时绑定给两个实例。

本机制明确区分四类标识：

| 名称 | 所属范围 | 是否由 mTLS 创建 |
| --- | --- | --- |
| `jiuwenclaw_id` | Manager 业务实例主键 | 否 |
| `GATEWAY_INSTANCE_ID` | Gateway 主备、Redis Key 等运行身份 | 否 |
| Runtime `instance_id` | Runtime 进程或副本身份 | 否 |
| `mtls_deployment_id`、`mtls_binding_id`、`mtls_binding_epoch` | 证书部署连续性及安全绑定 | 是 |

mTLS 不读取或改写前三类既有标识，也不提供另一套客户可配置的实例变量。`mtls_deployment_id` 仅保存在内部 profile 和绑定状态中；请求鉴权只发送带 `Mtls` 前缀的绑定 ID 与代次 Header。

### 5. 哪些设置由工具管理

普通部署只需显式开启 `JIUWENSWARM_LINK_MTLS_MODE=enforce`；证书相关信息由工具从已有部署参数和数据库派生：

| 配置类别 | 来源与处理 |
| --- | --- |
| 业务数据库、服务名、命名空间、端口 | 复用原部署参数 |
| mTLS 部署 ID、绑定 ID、绑定代次 | 首次随机生成并落库，后续读取数据库中的现有值；不由客户填写 |
| CA 公共证书、角色证书及私钥 | 首次生成，之后从数据库恢复原材料 |
| AgentServer Secret、headless Service、集群 DNS 域 | 部署层生成并注入 Runtime，由 Runtime 用于创建 AgentServer |
| 内部 `profile.json` 及必要的 `JIUWENSWARM_LINK_MTLS_PROFILE` 路径 | 工具管理的内部身份文件，不是客户额外手写的配置文件 |

不要通过手填 mTLS 部署/绑定 ID、提高 epoch、替换 profile 或删除表的方式解决身份冲突。这些值不是客户环境变量，也不复用 `GATEWAY_INSTANCE_ID` 或 Manager 的 `jiuwenclaw_id`。

### 6. 如何确认生效

确认部署完成且服务就绪。正常部署日志中会出现公开摘要 `mTLS binding ready`，包含 mTLS 部署/绑定编号、角色证书指纹、到期时间和 `reused` 状态，不包含私钥。

- 首次准备成功：`reused=false`。
- 使用已有材料：`reused=true`，指纹、绑定代次和到期时间保持不变。
- 数据库、证书、Secret 或服务地址冲突：部署停止，不以明文 HTTP 继续完成。

可在 Gateway 容器中使用其自身身份检查内部 HTTPS 健康接口；下面使用默认服务名和默认配置端口：

```bash
kubectl exec -n example-app deployment/jiuwenclaw-gateway -- python -c '
import urllib.request
from openjiuwen_runtime.foundation.security.link_profile import load_service_identity
identity = load_service_identity("gateway")
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=identity.ssl_context()),
)
with opener.open("https://127.0.0.1:8775/api/v1/ready", timeout=5) as response:
    print(response.status)
'
```

预期为 `200`。该检查证明本机 HTTPS 健康路径可用，不代替跨组件业务验收。正常业务还应确认 Runtime 路由、AgentServer 调用及回复链路可用。

有合法 TLS 证书但绑定或角色不符合要求时，应用层返回 `403` 和 `LINK_BINDING_MISMATCH`，并记录拒绝原因。无证书、非受信证书或明文请求可能在 TLS/协议层被直接拒绝，不保证收到 HTTP 状态码或应用层日志。排查时不要导出完整 Secret、私钥、Token 或带密码的数据库连接串。

## 二、升级、重部署与故障处理

### 1. 普通升级复用什么

使用同一数据库和受管地址重新执行普通 `up`，会读取数据库中的现有材料，而不是每次生成一套新证书。

| 情况 | 当前行为 |
| --- | --- |
| 普通升级、重复部署 | 复用原私钥和证书，绑定 ID、epoch、到期时间不变 |
| 数据库材料完整，证书 Secret 缺失 | 从数据库创建缺失的同一份材料，不重新签发 |
| 材料已入库，后续资源安装失败 | 后续重试读取已提交材料，校验通过后继续 |
| Secret 与数据库不一致 | 停止，不默认选择一方覆盖另一方 |
| 受管地址或 SAN 要求与已有材料不符 | 停止，要求按受控迁移处理 |
| 证书过期、状态已撤销、材料不完整或版本不兼容 | 停止，不通过普通部署重新签发或重新激活 |
| 只有旧公开绑定记录或旧版证书材料 | 不自动接管，要求明确的迁移方案 |
| 数据库和可用备份均丢失 | 无法保证恢复原私钥与原身份 |

新增材料字段以兼容补列处理，不要求重建业务表。历史 Link-Auth 数据不作为本机制的授权依据，也不由本功能删除。

数据库备份和恢复必须纳入证书材料保护范围。不要用删除命名空间、Secret 或数据库来代替普通升级。

### 2. 有效期与续期限制

首次签发有效期为十个公历年；重复部署不会把到期时间向后延长。不同安装使用随机生成的私钥，不使用写死种子或固定公共私钥。

当前没有自动续期服务，也没有交付跨组件证书轮换、解绑重绑和撤销的一键协调流程。CA 私钥在首次生成后不持久保存；更换身份需要新的受控签发与信任更新流程，不能假设仍可用原 CA 在线续签。

发生私钥泄漏、临近到期或确需改变绑定时，应按维护方案协调数据库、Secret、信任关系和全部相关进程。仅编辑某个数据库字段或某份文件不能构成完整换证。对绑定或证书代次的变更，现有进程会拒绝不一致状态；不承诺透明热换证和存量会话无中断。

### 3. 关闭与回退

默认不启用，和已经启用后的回退是两种情况。已有 enforce 部署不能仅把一个组件改为 `off`；其他组件仍可能要求 HTTPS 和证书。

如确需回退，应在维护窗口协调所有相关组件、已创建的 AgentServer、调用方地址及部署清单。仅修改 `.env.custom` 不会立即更新现有进程。关闭功能不等于撤销或删除已有证书，不建议为回退清空证书数据库。

## 三、客户管理服务如何接入

客户可实现自己的管理服务，其角色仍称为 `manager`。需由受控交付流程为该服务提供对应的 manager 角色材料，仅授予实际需要的管理权限，不应向浏览器或普通用户发放该私钥。Manager 角色证书是服务身份，不属于某一个 `jiuwenclaw_id`；一个 Manager 可以保存多条实例绑定。

使用配套 Runtime foundation 的异步客户端示例：

```python
import asyncio
import httpx
from openjiuwen_runtime.foundation.security.link_profile import load_service_identity


async def main():
    identity = load_service_identity("manager")
    # binding 由客户 Manager 按 jiuwenclaw_id 从自己的绑定库中读取。
    binding = load_instance_binding("customer-instance-id")
    url = identity.endpoint(binding.gateway_url, role="gateway")
    headers = {
        "X-Jiuwenswarm-Mtls-Binding-Id": binding.mtls_binding_id,
        "X-Jiuwenswarm-Mtls-Binding-Epoch": str(binding.mtls_binding_epoch),
    }
    async with httpx.AsyncClient(
        trust_env=False,
        timeout=10,
        **identity.client_kwargs(
            role="gateway",
            expected_fingerprints={binding.gateway_cert_fingerprint},
            ca_file=binding.trust_bundle_file,
        ),
    ) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        print(response.status_code)


asyncio.run(main())
```

前提是该进程已通过受控挂载取得 Manager 身份和目标实例信任包；示例中的 `load_instance_binding` 是客户管理服务自己的持久化读取逻辑，不由 JiuwenSwarm 提供。此代码不负责颁发证书或从数据库导出私钥。业务配置下发继续使用对应接口的原有业务请求结构。

使用其他语言或 TLS 客户端时，须同时落实：服务端 CA 与 SAN 校验、目标角色指纹校验、正确的客户端角色证书和绑定头。不得仅校验“同一个 CA”或仅转发几个 Header。

每条绑定至少需要保存 `jiuwenclaw_id`、`mtls_binding_id`、`mtls_binding_epoch`、Manager/Gateway/Runtime/AgentServer 证书指纹和信任包引用。Manager 发起配置写请求时，必须同时按实例选择绑定 Header、目标信任包和目标角色证书指纹，不能复用另一实例的绑定信息。

Manager 与目标实例可使用共同受控 CA，也可以为每个实例挂载独立信任包；无论哪种方式，目标 Gateway/Runtime 都必须明确信任该 Manager 证书，Manager 也必须按绑定记录精确校验目标叶证书指纹。只使用一个全局 CA 而不做实例级目标指纹校验，不满足本设计。

仓库内置 Manager 的参考登记接口为：

```text
PUT /api/v1/instances/{jiuwenclaw_id}/link-binding
```

请求中的 `{jiuwenclaw_id}` 必须是 Manager 已创建的业务实例 ID；接口不会修改该 ID，也不会把内部 `mtls_deployment_id` 写回 `instance_info`。请求体结构如下，示例值均为占位值：

```json
{
  "mtls_binding_id": "deployment-binding-id",
  "mtls_binding_epoch": 1,
  "mtls_gateway_endpoint": "gateway-service-a:8775",
  "mtls_runtime_endpoint": "runtime-service-a:8091",
  "manager_cert_fingerprint": "64位SHA-256小写十六进制指纹",
  "gateway_cert_fingerprint": "64位SHA-256小写十六进制指纹",
  "runtime_cert_fingerprint": "64位SHA-256小写十六进制指纹",
  "agentserver_cert_fingerprint": "64位SHA-256小写十六进制指纹",
  "trust_bundle_ref": "file:///受控挂载路径/ca.crt",
  "updated_by": "operator"
}
```

登记顺序分为两类：

1. 同一部署工具共同安装的 Manager、Gateway、Runtime：Manager 保持随机生成实例 ID；首次配置同步按 Gateway/Runtime 地址严格匹配部署 profile 后自动登记同一 binding，不要求人工复制 ID。
2. 其他受管实例：先让 Manager 使用其已安装的信任链完成只读健康探测，再为 Manager 返回的业务实例 ID 调用绑定登记接口，最后执行该实例的配置同步。若目标使用独立 CA，必须在健康探测前把对应 CA 纳入受控信任配置；不得为通过探测而关闭 TLS 校验。

绑定登记是幂等的：协议差异不影响地址匹配，登记时统一按 `host:port` 校验并保存；同一实例、同一 binding 和同一批证书材料可安全重放。地址不是合法 HTTP(S) 服务 authority、与 `instance_info` 不一致，或活动绑定的任一其他字段不同，均返回冲突。Manager 实际发请求时还会再次确认请求目标与绑定地址一致。解绑会递增 epoch 并释放 Gateway/Runtime 唯一占用，但数据库状态变化本身不会让已装载的证书立即失效，仍须配套完成材料替换和进程重启。

## 四、实现原理

### 1. 部署时的证书准备

1. 部署工具读取已有服务、数据库和命名空间参数，预检已有证书资源及服务形态。
2. 使用配套 Runtime 镜像在部署节点执行一次性处理命令，不新增独立的证书管理 Pod 或 Runtime 签发任务。
3. 首次安装生成绑定编号、CA 和四种角色材料；已有安装读取并校验数据库材料。
4. 先提交 Gateway 数据库中的完整材料，再同步 Runtime 公开绑定状态，随后创建缺失的 Kubernetes Secret。
5. 渲染证书挂载、受管 HTTPS 地址和携带身份的探针，再进入原有组件部署与业务配置流程。

代码职责沿用项目现有分层：`deploy/enterprise/link_mtls_handler.sh` 和 `templates/link-mtls.template.yaml` 负责部署编排与清单渲染；Runtime 的 `foundation/security` 负责证书、绑定和数据库材料处理。一次性材料命令在配套镜像内执行，不是部署目录中的独立 Python 脚本，也不是常驻服务。

Gateway 与 Runtime 数据库、Kubernetes 资源之间没有全局事务。上述顺序通过“先提交、后发布、重试复用、冲突停止”减少身份分叉，不表示任意中途失败都能无需检查地跨系统回滚。

### 2. 服务角色与请求授权

| 调用目标 | 允许的角色/用途 |
| --- | --- |
| Gateway 配置接收接口 | `manager` |
| Runtime 会话 route、touch、cleanup、config_refresh | `gateway` |
| Runtime 配置下发接口 | `manager` |
| AgentServer 业务接口 | `gateway` |
| 约定的内部健康接口 | 已绑定的 `manager`、`gateway`、`runtime`；仍要求有效客户端证书 |

四种角色各用不同私钥。`agentserver` 角色证书用于服务端；同一绑定中的多个 AgentServer 可以共享这份角色证书，表达的是角色身份而非每个 Pod 的唯一身份。

每个请求先完成 TLS 双向证书校验，再校验实际 TLS 对端证书的角色指纹，以及以下绑定信息：

| Header | 含义 |
| --- | --- |
| `X-Jiuwenswarm-Mtls-Binding-Id` | 当前部署绑定编号 |
| `X-Jiuwenswarm-Mtls-Binding-Epoch` | 当前绑定代次 |

这些 Header 不能代替证书，且不承载业务实例 ID。Manager 的 `jiuwenclaw_id` 继续留在原有 API 路径或业务请求中，只用于选择 `instance_link_binding`；目标必须同时匹配所选记录的 `mtls_binding_id`、`mtls_binding_epoch`、Manager 证书角色和证书指纹。知道实例编号、篡改 Header，或者持有同 CA 下其他角色的证书，不等于获得目标接口权限。服务端使用真实 TLS 对端证书，不信任请求自行声明的代理证书头。

SSE 存续期间也会重新检查绑定资料；撤销或信任变化触发拒绝/关闭。此机制是进程读到有效变更后的拒绝能力，不等于已实现向所有进程自动分发撤销的控制服务。

### 3. HTTPS 地址与动态 AgentServer

部署工具只协调声明过的受管服务地址。已声明的 Gateway/Runtime 地址由解析器在 enforce 模式下使用同端口 HTTPS，不需要逐项手工替换受管 URL。

未声明的外部地址必须显式为 `https://`，并通过对应 CA、SAN 和角色指纹校验。浏览器入口、模型厂商 API、外部 A2A 服务和用户登录服务不会被本功能全局改写成 HTTPS。

Runtime 通过 headless Service 下的 Pod DNS 返回 AgentServer 地址，证书 SAN 覆盖该 DNS 范围，而不是预先登记动态 Pod IP。不能通过关闭 SAN 校验解决域名不匹配。

### 4. Runtime 如何注入材料

Runtime 创建 AgentServer 时，向主容器注入 AgentServer 专用 Secret 和身份配置，不向 JiuwenBox 注入这份证书，也不会把 Gateway 或 manager 私钥作为普通业务环境变量传给 AgentServer。

保留原有 command、args、业务卷和权限配置。在需要适配非 root 或 fsGroup 的情况下，同一 Pod 内可使用受限的初始化/同步辅助容器，把投影材料复制到受控的内存卷；它们只处理文件副本，不签发、不续期，不是新的证书管理服务。

## 五、数据保存与安全边界

### 1. 保存位置

| 位置 | 保存内容 |
| --- | --- |
| Gateway DB 的 `link_binding_state`，`service_role=gateway` 记录 | 当前公开绑定及可恢复的完整角色材料 |
| Runtime DB 的 `link_binding_state`，`service_role=runtime` 记录 | Runtime 公开绑定、指纹、代次和 AgentServer Secret 引用；不保存整套角色私钥 |
| Kubernetes Secret | `jiuwenswarm-link-gateway`、`jiuwenswarm-link-runtime`、`jiuwenswarm-link-agentserver`、`jiuwenswarm-link-manager`，各保存对应角色材料 |
| Pod 内文件 | 角色私钥、证书、CA 公共证书及内部 profile 的只读挂载/受控副本 |
| 可选 Manager 的 `instance_link_binding` | 每个 Manager 业务实例到部署绑定、目标角色指纹和信任包引用的映射；普通部署恢复不依赖 Manager 数据库 |

没有要求客户额外维护主机持久证书目录；不表示证书只存在数据库中，运行时仍需要 Secret 和文件副本。

### 2. 数据结构说明

复用功能表 `link_binding_state`，不创建另一套并行的证书材料表。主要字段包括：

| 字段 | 作用 |
| --- | --- |
| `service_role` | 本服务记录与角色 |
| `mtls_deployment_id`、`mtls_binding_id`、`mtls_binding_epoch` | mTLS 内部部署连续性、绑定编号与绑定代次；均不复用业务/进程实例 ID |
| `protocol_version` | 绑定协议结构版本，与软件版本号、证书有效期不同 |
| `local_cert_pem`、`local_cert_fingerprint` | 本服务公开证书和指纹 |
| `peer_trust_bundle_pem`、`private_key_ref` | CA 公共材料和运行时私钥引用 |
| `agentserver_secret_ref`、`status` | AgentServer 材料引用和绑定状态 |
| `material_schema_version`、`material_epoch` | 可恢复材料格式及对应代次 |
| `certificate_materials` | Gateway 中的完整角色材料；TEXT/MySQL LONGTEXT，不作为普通业务响应返回 |

首次使用创建功能表。如果业务进程已先创建同名的公开状态投影，部署阶段只为该功能表补充可空的材料列，不重建业务表。现有记录缺少可恢复材料、版本不兼容或与当前身份冲突时停止，不能把“记录不完整”当成新安装并直接重签。

本设计不创建、读取、写入或迁移 `link_identity`。如测试环境曾运行未合入的早期实现并留下同名表，当前代码会直接忽略它；部署工具不会主动删除未知历史表。

应用启动按公开状态核对 mTLS ID、指纹、epoch 和 active 状态；这些 ID 不作为客户环境变量，服务也不能自行覆盖数据库授权状态。

### 3. 私钥与运维权限

数据库在本方案中是可信的秘密保存边界。`certificate_materials` 包含可恢复私钥，没有由应用额外加密；读取敏感列、数据库备份或相应 Secret 的主体可能取得服务身份。Kubernetes Secret 的 Base64 编码也不是加密。

需要限制数据库账号、备份访问、Secret RBAC 和部署执行权限，并按环境要求配置数据库传输与存储加密。普通 API 不返回私钥，不等于数据库管理员无法读取私钥。十年有效期是运维取舍，不代表内部网络不存在泄漏风险。

角色私钥隔离不等于节点 root、特权容器或整个数据库失陷后的防护。当前方案也不代替用户登录、业务级用户/组织/Agent 授权、外部 A2A 访问控制和模型平台鉴权。

### 4. 当前支持与限制

支持首次十年签发、数据库持久化、普通升级复用、受管材料恢复、HTTP/SSE 双向证书与角色/绑定验证及拒绝日志。

不提供自动续期、旧材料自动导入、跨组件一键轮换/解绑/撤销编排，也不承诺透明热换证。多实例 Manager 的绑定选择和精确目标指纹校验由协议与参考实现支持；远端实例的证书颁发、信任包挂载、首次登记及高可用切换仍需结合客户拓扑和运维体系完成。
