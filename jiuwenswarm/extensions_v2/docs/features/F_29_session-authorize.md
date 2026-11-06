# session.authorize / ops.session.authorize 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_29 / session-authorize |
| 最近修订 | 2026-11-04 |
| 主责规约 | [S_16_session-authorize.md](../specs/S_16_session-authorize.md) |

## 1. 范围与前置条件

本文只说明 `ops.session.authorize()` 的调用契约及同名 `session.authorize` 操作事件。Ops 无参数，返回 `{handle, kind}` 或 `None`；事件允许 USER Handler 放行、隐藏或拒绝。

**宿主显式绑定是签发前置条件。** Worker 扩展装配后，宿主须把已取得的 Anthropic 或 OpenAI 凭据绑定到当前会话；默认没有绑定，CORE 返回 `{ "value": None }`。插件不能通过 Ops 传入凭据。当前没有自动凭据来源或句柄消费入口。

## 2. Ops 与事件概览

方法为当前受管包 Handler 获取“当前会话已由宿主托管的 Anthropic 或 OpenAI 凭据”的不透明引用。真实凭据只保存在宿主内存中；用户既不传凭据，也不指定服务、权限范围、会话或包身份。没有符合条件的绑定时返回 `None`。

例如宿主给会话 A 绑定了一个 API Key，插件调用后取得 `{"handle": "sa_<宿主随机签发值>", "kind": "api-key"}`。handle 是平台记录的查找编号，关联原调用插件、会话、激活代次和那份凭据；字符串本身不包含 API Key。kind 只说明凭据类别，不证明已登录、已调用外部服务或已完成权限审批。当前没有配套 HTTP 消费能力，后续消费必须由宿主实现。

同名事件允许 USER/BUILTIN 观察和限制这次方法调用：`next_handler(event)` 取得默认结果，`{"value": None}` 隐藏本次结果，`{"deny": "原因"}` 拒绝。CORE 只在可信调用帧中向宿主服务申请句柄；Handler 不能捏造 handle/kind 来取得凭据。事件封装在 Ops 返回时解包。

| 项 | 契约 | 使用说明 |
| --- | --- | --- |
| `ops.session.authorize()` | 无参数，返回 `{handle, kind}` 或 None | `kind` 只能是 `bearer` 或 `api-key`；handle 是不透明引用 |
| `session.authorize` 事件 | 空映射输入，返回 `{value: ...}` 或 `{deny: str}` | Ops 解包 `value`；Handler 通过 `next_handler(event)` 继续当前事件链 |
| 宿主显式绑定（签发前置条件） | Worker 宿主先调用 `get_authorization_service().bind(SessionContext(session_id), provider=..., credential=..., kind=...)` | 支持 Anthropic 和 OpenAI；未绑定时默认 CORE 返回 None |
| 凭据归属 | 真实凭据只留在宿主内存中 | Event、SessionContext 和 Ops 结果不携带密钥 |

## 3. 事件契约

### 3.1 触发前后与粒度

```text
Worker 启动：登记 session.authorize / 校验 / 唯一 CORE
  → ops.build 的 BUILTIN 把 authorize 代理加入共享 Builder
宿主按真实凭据生命周期显式 bind(SessionContext, provider, credential, kind)
平台已有业务入口收集可信 SessionContext → 触发外层业务事件
  → EventBus 使用已保存 Builder，为实际 USER Handler 创建 Ops 并绑定会话和包
  → await ops.session.authorize()
  → Runtime 校验当前 Ops / Task / 活动期；invocation_generation(ops) 提供原调用包代次
  → 私有调用帧 → bus.trigger(Event("session.authorize", {}), SessionContext)
  → USER：放行 / 隐藏 / 明确拒绝 → 可选 BUILTIN → session_authorize_core
  → authorize_core：核对私有调用帧、会话和空输入
  → service.issue：按 session_id 查询宿主绑定，签发并登记 handle；无绑定返回 None
  → CORE 返回 {value: {handle, kind}} 或 {value: None}
  → BUILTIN/USER 的 next 之后仍可隐藏 / 拒绝
  → 宿主最终核对句柄、原调用身份和绑定代次；未保留结果时撤销本次句柄
  → Ops 解包 value → 原 USER 使用结果，再 next 原业务事件
```

只在方法调用时触发，一次方法一条独立链；不依赖 notify_context_usage，也不在每轮或启动时自动执行。现成示例把方法调用放在 context.usage.updated 的 on_usage 中，因而在该示例的用量通知时申请引用；其它有有效会话的活动受管 Handler 同样可用。Core 缺活动调用校验时，在触发内层事件前返回 None；Core 能力齐全但宿主没有绑定时，仍执行事件链，由 CORE 返回空结果。

自行 trigger 没有可信私有调用帧会被拒绝；这种直接调用可表现为 Core ValidationError。用户在 `session.authorize` Handler 内通过 next 继续当前链，不再调用同名 Ops，以免 AUTHORIZATION_REENTRANT。普通 USER 异常或伪造结果可能被 Core 的 USER 故障隔离跳过，主动拒绝必须返回合法 deny；不能把抛异常当成可靠拒绝。

### 3.2 输入：每个字段表达什么、由谁填写

事件契约和公开类型位于 `jiuwenswarm.extensions_v2.event_types.session_authorize`。方法无参，事件的业务输入严格为空映射。下面是**平台内部触发形态示意**，用户只调用 Ops，不自行构造这次私有调用：

```python
await bus.trigger(
    Event("session.authorize", {}),
    bound_session_context,
)
```

| 字段 | 类型 / 是否总有 | 准确含义 | 应该填写什么、示例 | Handler 权限 |
| --- | --- | --- | --- | --- |
| event.event_type | str，必有 | 当前处理的操作事件名称 | **平台填写** session.authorize；用户注册时也使用这个名字 | next 时保持不变 |
| event.event_input | 空 Mapping，必有 | 无业务参数的调用输入 | **平台填写** `{}`；USER 正常原样 `next_handler(event)` | 不增加 service、scopes、session_id 等字段 |

**会话、凭据和包身份从哪里来：**以下对象是调用上下文，不属于 event_input，也不由用户填写授权参数。

| 上下文项 | 来源 / 类型 | 准确含义与使用方式 |
| --- | --- | --- |
| ops.session_context.session_id | 平台业务入口提供的 SessionContext 中的非空 str | 确定查询哪一个会话的宿主绑定，例如 session-a；不是由 USER 选择的目标参数 |
| ops.package_metadata | Core 为当前实际 Handler 绑定的包资料；CORE 的包资料为空 | 表示正在执行哪个 Handler 的包。内层策略 Handler 的包身份与外层申请插件身份可能不同，不能混为同一申请者 |
| 私有调用帧 caller | 方法服务根据 Runtime 核验得到的 AuthorizationCaller | 保存最初调用 Ops 的插件及其原激活代次；CORE 给这个发起者签发，不能改成内层策略包或重载后的新实例 |
| 宿主凭据绑定 | 宿主先调用 service.bind 保存的私有对象 | 用 session_id 查询；真实 credential 留在服务内存，SessionContext 只提供会话身份，不负责携带密钥 |

matcher 使用 `{}` 匹配全部授权调用。因为没有业务输入字段，不提供按 service/scopes/provider 筛选的契约；本项目也未提供 next.origin。无需为这个方法补齐 cwd、root、model 或用量数据。

### 3.3 输出：句柄、空结果与拒绝

默认 CORE 返回 value 封装，正常 Handler 原样返回 `await next_handler(event)`。两种顶层分支互斥，只能有 value 或 deny，不能添加 success、token 等其它键。

| 字段 | 类型 / 返回时机 | 含义与调用方怎样使用 |
| --- | --- | --- |
| value | SessionAuthorization 或 None；非拒绝分支必有 | 本次方法结果。通常保留 next 返回的对象；USER 想隐藏本次授权时，可在 next 前或后返回 `{"value": None}` |
| value.handle | 非空 str；value 非 None 时必有 | 宿主签发的临时凭据引用，例如示意 `sa_<宿主随机值>`。只能保留真实 CORE 返回的值，不手写、改写或拼接；它不包含 API Key，也不用于替代 HTTP Authorization 头 |
| value.kind | bearer / api-key；value 非 None 时必有 | 宿主绑定的凭据类别：bearer 对应 Bearer 类凭据，api-key 对应 API Key。由 CORE 填写；USER 保持原值，不填 token、Bearer 或 api_key |
| deny | str；拒绝分支必有 | USER 填写的明确拒绝原因，去首尾空白后 1～512 字符，如 `{"deny": "本插件配置关闭会话授权"}`。Ops 调用者收到 SessionAuthorizationDenied，code 为 DENIED，原因在 reason 属性中 |

None 是正常空结果，不区分无绑定、到期、不支持 provider、缺 Core 能力或 USER 主动隐藏；不能据此宣称登录完成。deny 表示策略拒绝，不转换为 None。最终结果必须对应本次实际签发记录；伪造或跨会话句柄会被拒绝或由 Core 的 USER 故障隔离跳过，不能据此取得凭据。CORE 签发后若整条链最终隐藏、拒绝、取消或失败，本次句柄会撤销。重复 next 至多签发一次。

### 3.4 注册、筛选、改写示例

下面是用户包示例。on_usage 是外层业务 Handler，负责申请并使用结果；on_authorize 是内层授权策略 Handler，负责放行、拒绝或隐藏。两个布尔开关是**示例插件自己的配置**，不是 Ops 参数或平台注册字段。

```python
import logging
from jiuwenswarm.extensions_v2.event_types.session_authorize import (
    SessionAuthorizationDenied, SessionAuthorizationError,
)

logger = logging.getLogger(__name__)

class Extension:
    def __init__(self):
        self.allow_authorization = True
        self.hide_authorization = False

    def register(self, registrar):
        registrar.register_event_handler("context.usage.updated", self.on_usage, {})
        registrar.register_event_handler("session.authorize", self.on_authorize, {})

    async def on_usage(self, event, ops, next_handler):
        try:
            value = await ops.session.authorize()
        except SessionAuthorizationDenied as error:
            logger.info("Authorization denied: %s", error.reason)
        except SessionAuthorizationError as error:
            logger.info("Authorization unavailable: %s", error.code)
        else:
            logger.info("Authorization available=%s kind=%s", value is not None,
                        None if value is None else value["kind"])
        return await next_handler(event)

    async def on_authorize(self, event, ops, next_handler):
        if not self.allow_authorization:
            return {"deny": "本插件配置关闭会话授权"}
        result = await next_handler(event)
        if self.hide_authorization:
            return {"value": None}
        return result

def create_extension():
    return Extension()
```

本事件没有可改写的业务输入，示例的“改写”指结果隐藏或拒绝；不能在此构造新的有效 handle。next 继续当前授权链，Ops 调用才会另起授权事件。用户把 manifest.json 与模块放在包根目录，通过既有管理入口安装。平台负责事件触发、会话事实和宿主绑定，用户不需要改原业务方法或采集真实凭据。

## 4. 方法契约

### 4.1 ops.session.authorize()

```python
async def authorize() -> SessionAuthorization | None: ...
```

零参数；位置参数和关键字参数均不接受。返回字典只有 handle/kind 两个键，或 None；没有 accepted、success、token、expires_at、service、scopes 等输出字段。无宿主绑定、绑定过期、provider 不支持或构建缺少活动调用校验时返回 None，不把这些情况伪造为授权成功。

推荐在活动受管 USER Handler 内调用并先处理 None：

```python
authorization = await ops.session.authorize()
if authorization is None:
    available = False
else:
    available = True
    credential_kind = authorization["kind"]
    host_handle = authorization["handle"]
```

| 输入 / 输出项 | 是否由用户填写 | 准确含义、填写方式及例子 |
| --- | --- | --- |
| 方法业务参数 | 不填写 | 直接调用 authorize()；不传 {}、service、scopes、credential、provider、session_id 或目标插件 |
| 当前会话与调用身份 | 平台绑定，用户只读 | 使用本次 Handler 的 Ops；不在 Handler 返回后缓存该 Ops 再调用，也不把它交给 asyncio.create_task 借用 |
| 返回值 handle / kind | 宿主填写，用户读取 | 方法返回裸字典，例如 `{"handle": "sa_<宿主随机值>", "kind": "api-key"}`；字段定义同 3.3 去掉 value 前缀，不能把字典当作有 .handle 属性的对象 |
| 返回值 None | 平台或合法 USER 隐藏结果 | 分支判断后再访问字段；表示本次没有可用引用，后续业务可自行继续 |

在支持的构建中，无效会话/非受管或过期 Ops、跨任务借用、重入及非法最终结果是明确错误；deny 映射为携带用户原因的 SessionAuthorizationDenied。取消传播 CancelledError。None 是正常结果，不能直接访问其 handle；它不区分无绑定、不支持、到期或 USER 主动隐藏，不代表本方法完成了一次登录。

## 5. 宿主绑定与可观察边界

本节只记录 `ops.session.authorize()` 签发句柄所需的宿主前置条件，以及调用方能观察到的结果；宿主凭据来源和后续消费不属于此 Ops/Event 契约。

### 5.1 宿主绑定输入：谁提供、填什么

Worker 扩展装配完成后，宿主以平台确认的 `session_id` 显式绑定可信凭据。插件不传入凭据，也不从事件输入选择会话。

```python
service = get_authorization_service()
service.bind(
    SessionContext("session-a"), provider="openai",  # or "anthropic"
    credential=trusted_credential, kind="api-key", expires_at=None,
)
# Revoke when the host session or credential ends.
service.unbind("session-a")
```

`provider` 支持 `anthropic` 和 `openai`，`kind` 支持 `bearer` 和 `api-key`。`bind()` 成功只表示当前 Worker 保存了会话绑定，不证明外部服务已接受凭据。未绑定时默认 CORE 返回 `{ "value": None }`。

### 5.2 句柄生命周期

同一会话一次只保留一份绑定；重新绑定或 `unbind()` 会撤销旧句柄。非空句柄属于原调用包代次和会话，仅在当前 Worker 内有效，默认最长 300 秒，也不晚于宿主提供的凭据到期时间。`session.authorize` Handler 最终隐藏或拒绝本次结果时，宿主撤销已签发的句柄。句柄不是密钥，不能当作 HTTP Authorization 头使用。

### 5.3 失败、取消和可观察结果

| 结果 / 异常 | 判定条件与调用方应怎样处理 |
| --- | --- |
| None | 无有效宿主绑定、provider 不支持、绑定过期、Core 能力不足或 USER 合法隐藏；这是正常结果，不当作授权成功 |
| SessionAuthorizationDenied，code=DENIED | 内层 USER 返回合法 deny；调用方可以读 reason 解释拒绝，或继续自己的其它业务；本次已签发句柄撤销 |
| SessionAuthorizationError，code=UNBOUND_INVOCATION | 受管调用、Ops、Task、会话、原插件代次或活动期不符合要求；不通过缓存 Ops、复制上下文或自建 Builder 绕过 |
| AUTHORIZATION_REENTRANT | 已在当前授权事件内再次调用同名 Ops；内层 Handler 应通过 next 继续 |
| INVALID_RESULT | 最终结果不再对应有效宿主记录，例如 CORE 返回后绑定已被解除；不把过期引用作为成功结果交给调用方 |
| HANDLE_CAPACITY_EXCEEDED | 清理到期项后仍达到服务实例的句柄上限；不静默签发更多引用 |
| SERVICE_CLOSED | 授权服务已经关闭；需要由正常宿主生命周期重新装配，用户不重新打开旧服务 |
| TypeError | Ops 收到任意位置或关键字参数；调用方改为无参形式 |
| Core ValidationError | 手工触发事件缺少可信调用帧等入口校验失败；使用活动 USER Handler 中的 Ops 方法 |
| asyncio.CancelledError | 本次异步调用取消，按原取消路径传播；本次已签发而未保留的句柄撤销 |

失败码通过 SessionAuthorizationError.code 读取，不把堆栈或真实凭据当作错误字段。USER 的非法输出或未捕获异常沿用 Core 故障隔离；外层业务仍继续并不能证明授权成功，业务调用者应检查 Ops 的真实返回或捕获异常。本方法只修改宿主内存中的授权引用，不写入产品历史、触发模型或渲染 UI。

## 6. 验证与边界

2026-11-04 在 Server 目标基线 `39fdb7354` 上仅集成授权改动，使用实际 Core `31fee3a86` 验证。授权专项 35 项通过；连同 session.start 装配兼容回归共 53 项通过，覆盖无绑定、Anthropic/OpenAI 绑定、USER 放行/隐藏/拒绝、无效句柄、过期、重载与撤销。

独立进程将仓库示例打成 ZIP 后真实安装，并把第 3.4 节代码提取为用户包验证放行、拒绝和隐藏。共 8 次授权事件返回正确，外层用量回执保留，卸载和停机清空句柄及凭据绑定；没有 ui.render 调用。

Extensions V2 全组为 933 通过、2 跳过、7 失败。7 项失败已在未修改的同版本 Server 目标基线与相同 Core 上复现：5 项 compact 测试的旧 Runtime 私有接口调用不兼容，1 项 Store Probe 未到达，1 项 Workbench 未得到历史调用。它们保留为目标基线问题，本 PR 未将全组记为通过。

测试使用占位凭据；未验证真实凭据、自动采集、HTTP 消费或网络请求。`{handle, kind}` 只证明当前宿主签发了受管引用。

## 7. 参考资料

- [规约 S_16](../specs/S_16_session-authorize.md)。
- [事件定义与校验](../../event_types/session_authorize.py)、[默认 CORE](../../event_handlers/session_authorize.py)、[Ops 登记](../../handler_ops/session_authorize.py)。
- [宿主绑定与签发](../../session_authorization/service.py)、[事件触发与结果解包](../../session_authorization/dispatch.py)。
