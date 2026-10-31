# ops.session.cwd / ops.session.root 特性设计

# session.cwd / session.root 与会话工作区 Ops 特性设计

## 1. 范围、状态与依据

本分支已实现。最近修订 2026-10-31，维护者刘瑜；本次基于已合入 Server `c6a07f909` / Core `bdde48a8`，不依赖未合入的生命周期 PR。公共机制以 [S_16](../specs/S_16_session-query-events.md) 为准，专项字段主责为 [S_03_session-workspace-ops.md](../specs/S_03_session-workspace-ops.md)；本文字段表提供填写与消费解释，不另立契约。

本次按用户确认的方向，以 Claude Code Mods 2.1.277 的返回形态优先；固定参考提交 `80c6959d28c4c30ecd14fc1cdf172f19d9adad81`。此前直接读实现已改为查询事件链；旧验证不能代替本次事件化验证。统一实施与最终证据见 [F_27](F_27_session-query-events.md)。

## 2. 能力说明与 Claude 对照

`await ops.session.cwd()`：查询本次 Handler 所绑定会话的执行工作目录，适合生成工作区摘要或对读取行为做审计。它不切换目录、不检查目录是否仍存在，也不读取 Worker 的进程当前目录。

`session.cwd` 在每次该 Ops 调用时触发，Handler 接收查询参数，可继续、加工独立返回视图、短路或拒绝；默认 CORE 完成读取，结果由原 Ops 调用者消费。它不是状态变化通知，也不会自动渲染或写入记录。

`await ops.session.root()`：查询本次绑定的用户项目根，适合展示项目归属。它不自动等于 cwd、Git 根或内部任务工作区；无项目会话可以有 cwd 而 root 为 None。

`session.root` 在每次该 Ops 调用时触发，Handler 接收查询参数，可继续、加工独立返回视图、短路或拒绝；默认 CORE 完成读取，结果由原 Ops 调用者消费。它不是状态变化通知，也不会自动渲染或写入记录。

| 能力 | 默认 CORE 数据来源 | 公开返回 | 与 Claude 的差异 |
| --- | --- | --- | --- |
| session.cwd | SessionContext.cwd；经本机绝对路径校验。 | str 或 None | Claude 返回 string；本地在平台没有目录资料时保留 None，不伪造当前进程目录。 |
| session.root | SessionContext.metadata['project_root']；未知为 None。 | str 或 None | Claude 返回 string；本地无项目时保留 None。 |

## 3. 事件契约

### 3.1 触发位置与完整流程

```text
示例入口：原生回合完成，平台收集可信会话资料
  → notify_context_usage(...) 安排 context.usage.updated
  → Worker EventBus 给实际 USER on_usage 绑定本次 Ops
  → USER 调用 ops.session.cwd() / ops.session.root()
  → 查询代理：验证参数与会话，固定原调用包，建立循环保护
  → 同一 Worker EventBus.trigger(同名查询 Event, 原可信 SessionContext)
  → 查询 USER：前置操作 → next_handler(event)
  → 查询 BUILTIN：计时 → next_handler(event)
  → 查询 CORE：直接读取平台事实或服务，不调用同名公共 Ops
  ← {'value': 查询结果} / {'deny': 原因} 逐层返回
  ← 代理验证、复制、解包；拒绝转 SessionQueryDenied
  ← 外层 USER 使用值；按原事件契约 next，继续原通知 CORE
```

这个入口只是现成示例，不是查询前置依赖。任何已由平台绑定相应会话的 Handler 都可调用；用户不用修改 Adapter、创建 EventBus 或先调用 notify 取得上下文。Front/Supervisor 不新增 EventBus。

### 3.2 事件输入、上下文和调用者

| 对象 | 类型 | 谁提供 | 含义 | 如何使用 |
| --- | --- | --- | --- | --- |
| ops.session_context | Core 只读属性 | 平台在业务边界提供，Core 规范化冻结 | 当前 Handler 的可信会话入口；不是 await 方法，不负责采集或组装数据 | 直接读取属性；不要把用户 payload 转成可信上下文 |
| SessionContext.session_id | str | 平台业务入口 | 当前会话身份；query 参数中没有这个字段 | session-a；用户不能通过修改 Event 输入换目标 |
| SessionContext.cwd / metadata | 只读快照 | 实际业务/运行时绑定 | 目录、项目根及可用主模型/用量快照；不要求装载全部历史或 Git 数据 | repo/messages/turns 可凭身份调用底层服务 |
| current_query_origin().package | str 或 None | 查询代理从原 Ops 的包身份固定 | 发起查询的包；不是当前查询 USER Handler 所属包。直接由宿主触发且无 Ops 调用域时 helper 返回 None | query.reader；不包含一个猜测的 tier |

### session.cwd：输入

| 字段 | 类型 / 默认 | 谁填写 | 含义与边界 | 怎样填 / 例子 |
| --- | --- | --- | --- | --- |
| 方法参数 | 无 | 调用者不填写 | 查询已绑定会话，不接受 session_id、路径、模型名或包 ID | 直接调用 await ops.session.<方法>() |
| Event.event_input | 空映射 {} | Ops 代理自动构造 | 查询 Handler 收到空参数对象；不是 SessionContext，也不携带可改写目标身份 | 注册时使用 match_pattern={} |

### session.cwd：返回值字段

| 字段 | 类型 / 出现条件 | 谁提供 | 准确含义 | 消费方式 / 例子 |
| --- | --- | --- | --- | --- |
| 方法返回 / value | str 或 None | 由平台 CORE 或查询 Handler 返回 | 执行目录绝对路径；None 表示这次绑定没有目录资料，不代表磁盘路径不存在 | D:/projects/demo/src；不是 {'cwd': ...} |

### session.root：输入

| 字段 | 类型 / 默认 | 谁填写 | 含义与边界 | 怎样填 / 例子 |
| --- | --- | --- | --- | --- |
| 方法参数 | 无 | 调用者不填写 | 查询已绑定会话，不接受 session_id、路径、模型名或包 ID | 直接调用 await ops.session.<方法>() |
| Event.event_input | 空映射 {} | Ops 代理自动构造 | 查询 Handler 收到空参数对象；不是 SessionContext，也不携带可改写目标身份 | 注册时使用 match_pattern={} |

### session.root：返回值字段

| 字段 | 类型 / 出现条件 | 谁提供 | 准确含义 | 消费方式 / 例子 |
| --- | --- | --- | --- | --- |
| 方法返回 / value | str 或 None | 由平台 CORE 或查询 Handler 返回 | 用户项目绑定的绝对根目录；无绑定为 None，不用 cwd 或 Git 根兜底 | D:/projects/demo；cwd 可以是 demo/src |

### 3.3 事件输出、拒绝与合法改写

| 事件结果键 | 类型 | 出现规则 | 含义 | 使用方式 |
| --- | --- | --- | --- | --- |
| value | 按方法定义 | 成功时唯一顶层键 | 下游或 CORE 给出的查询值；合法 None 仍须包装成 {'value': None} | 普通 Ops 调用已解包，不读取 result['value'] |
| deny | str，去空白后非空，最多 512 字符 | 拒绝时唯一顶层键 | 明确拒绝原因；不能与 value 同时出现，也不靠 raise 来代替 | return {'deny': 'WORKSPACE_QUERY_DISABLED'} |

普通 Ops 返回的是 value 本身，查询 Handler 的 next 返回的是封装结果。字段改写只改变本次查询视图，不会更换可信目标、切目录、切模型或改历史。CORE 和原始服务不使用可改写的 cwd/root/model 查询结果来重定向其他查询。

普通 USER/BUILTIN 异常及无效输出依 Core 隔离，默认读取可能继续；这不等于明确拒绝。需要拒绝就返回 deny，调用方捕获 SessionQueryDenied。取消继续传播，调用域 finally 清理；CORE/框架故障不冒充空值或成功。多个 Handler 按 USER→BUILTIN→CORE 及同层注册顺序进入，结果反向返回。多次 next 可能重复读取，不承诺查询默认缓存。

### 3.4 用户 Handler 注册示例

```python
class Extension:
    def register(self, registrar):
        registrar.register_event_handler("session.cwd", self.on_query, {}, "user")

    async def on_query(self, event, ops, next_handler):
        # 这里可记录审计、检查自己的规则；默认查询用 next，不能再次调同名 Ops。
        result = await next_handler(event)
        if "deny" in result:
            return result
        return {"value": result["value"]}  # 可按本方法契约加工独立返回副本
```

短路查询可直接 `return {"deny": "QUERY_DISABLED"}` 或 `return {"value": 合法值}`；不会执行后续默认查询。无参数查询使用空匹配模式；usage 可按 `breakdown` 等参数匹配。Matcher 在 trigger 时固定，next 改写参数不会重新匹配。

## 4. 方法契约

### 4.1 签名、调用场合与填写方式

```python
cwd = await ops.session.cwd()
if cwd is not None:
    logger.info("execution directory: %s", cwd)
```

```python
root = await ops.session.root()
logger.info("project root: %s", root)
```

### 4.2 返回值与错误

各方法字段和单位已在第 3.2 节逐项列出。返回副本可供用户加工，不写回平台，也不自动保存到 SessionContext。结果未知、空数组、真实零值和读取失败按对应方法区分；不得统一回填零或空字符串。

无 SessionContext 时 RuntimeError；会话身份非法由平台检查拒绝；非法方法参数 TypeError。显式拒绝为 SessionQueryDenied（code=DENIED、reason 是用户收到的公开原因）。嵌套同名或间接查询循环为 SessionQueryError(code=QUERY_REENTRANT)，深度超过 16 为 QUERY_DEPTH_EXCEEDED。调用参数不能覆盖 bus/event_type。平台具体读取错误继续保留其错误类型和稳定 code。

## 5. 接入与兼容方案

### 5.1 平台装配和业务调用链

bootstrap 注册本次同名查询类型、Matcher、Validator 和唯一默认 CORE；再注册真实查询 BUILTIN（计时与独立副本）以及 ops.build BUILTIN。ops.build CORE 只创建共享 Builder，BUILTIN 登记查询代理；每个实际 Handler 获得的是共享目录全部能力与当次可信上下文，不是只装载当前事件的一个方法。

实现分工：`event_types/session_queries.py` 定义、校验和注册；`handler_ops/session_queries.py` 创建封闭代理；`query_dispatch.py` 负责 trigger、调用者归属、重入和解包；`event_handlers/session_queries.py` 提供查询 BUILTIN/CORE；真实读取位于 `server/runtime/session/query_*`。新增查询定义模块不包含 trigger，本次不迁移既有 notify 入口的位置。

内部旧直接读取辅助函数/分页服务仍可供平台和存储回归使用，但不再登记为这七个公开 Ops。查询不创建 Agent、不发模型请求；用户包卸载移除其 USER Hook，不移除平台查询事件或默认 CORE。没有全局结果缓存；并发和嵌套会话分别绑定。

### 5.2 迁移清单

| 方法 | 本次需要调整什么 |
| --- | --- |
| 编号 | F_13 / session-workspace-ops |
| 建档日期 | 2026-10-26 |
| 后续修订 | 2026-10-29，统一使用契约、字段含义和填写示例；仅文档 |
| 关联功能 PR | PR01 [!71](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/71)，已合入 |
| 主责契约 | [S_03](../specs/S_03_session-workspace-ops.md) |

## 1. 范围、状态与依据

两项只读 Ops 已实现并合入。本次在最新 `rsi_native_agent_os` 的 7795851916dd5ce1d34b70f5a20d6d30eafc25e3 上统一说明，业务代码不变。最初解决的是宿主尚未开放目录查询的问题；该背景不是当前未完成状态。

范围是当前 Worker 中 Handler 所绑定会话的目录快照。它们不接收查询目标/路径，不根据进程目录或用户 payload 猜目录，也不触发新事件。历史与本轮文档验证分开记录在第 6 节。

## 2. 能力说明与 Claude 对照

`await ops.session.cwd()` 读取这次调用绑定的执行工作目录；`await ops.session.root()` 读取同次调用绑定的项目根目录。两者返回宿主平台的绝对路径字符串，未知为 None；读取不创建/切换目录，不检查路径此刻仍存在，不返回 Path 对象或包装字典。

Claude 固定版本的 `$.session.cwd()` / `$.session.root()` 同样提供目录读取。本地能力明确允许 None，root 只表示用户项目绑定，不能用内部任务工作区填充。对照只说明业务分工，不宣称 TypeScript 接口可直接互换。

## 3. 复用事件与上下文契约

本特性没有新增查询事件或查询 CORE。示例订阅既有 `context.usage.updated`；这只是一个已接线的业务触发入口，不是调用查询的前置操作。其他已有事件只要平台提供所需 SessionContext，其 USER Handler 同样可以调用。

| 对象 | 字段 / 来源 | 用户应如何使用 |
| --- | --- | --- |
| 原通知输入 | ContextUsageUpdate：session_id、request_id、input_tokens、output_tokens、total_tokens、cache_tokens、cache_hit_rate、context_window_tokens、usage_percent、model | 由平台汇总填写；不包含本特性查询的权威目录/模型/仓库返回值。仅用于观察和筛选，不能靠改 payload 重定向 Ops |
| 原通知输出 | ContextUsageOutput：accepted 为 bool；request_id 为 str | 默认 CORE 填 accepted=True 并回显请求 ID，仅确认输入类型已接受，不表示目录/模型/Git/UI 验收成功。用户通常原样 next 返回 |
| Ops 绑定 | Core 已有只读属性 ops.session_context | 平台随 Event 单独提供，Core 规范化/冻结后绑定到本次 Ops；不是本次新增的异步方法，用户不调用它来收集资料 |

原通知在已接线的原生流式请求末尾、有有效用量时由平台调度，Worker 内实际 trigger；不按每个 Token、工具调用或目录/模型变化触发。默认通知 CORE 只校验输入类型，没有因本次查询新增完整数值 Validator。用户保持输入并 `return await next_handler(event)` 继续原链；短路不撤销已完成回合，USER 异常依 Core 隔离规则处理。

完整通知定义以 [context_usage.py](../../event_types/context_usage.py) 为准。查询本身不会再 trigger、不会再次 ops.build，也不会把自己的返回值自动塞入原通知回执。


## 4. 方法输入输出与填写规则

### 4.1 输入：两个方法均无参数

```python
cwd = await ops.session.cwd()
project_root = await ops.session.root()
```

**用户不填写任何输入字段。**不要传 session_id、路径、项目 ID、cwd 初值或文件名。当前会话与目录由平台提供，不从用户 Event payload 里读取，也不从 Worker 的 os.getcwd() 兜底。

| 平台上下文字段 | 类型 / 缺省 | 准确含义、由谁填写 | 示例与权限 |
| --- | --- | --- | --- |
| SessionContext.session_id | str，会话绑定时提供 | 这次事件的会话关联身份；用户不靠修改 payload 更换它 | session-a；只读 |
| SessionContext.cwd | str 或 None，未提供为 None | 原生执行任务/平台入口在事件边界采集的工作目录，不是整个 Worker 的进程目录 | Windows 的 D:/projects/repo/app/src；只读 |
| SessionContext.metadata.project_root | str 或缺省 | 平台确认绑定的用户项目根目录；没有绑定则缺失/None | D:/projects/repo/app；只读，不自动补成 cwd |

平台需使用本机绝对路径语法：Windows 如 D:/projects/repo/app/src，POSIX 如 /home/me/repo/app/src。Ops 不展开 `~`、环境变量或相对路径，也不在读取时 normalize/resolve/修复原路径。

### 4.2 返回：整体就是字符串或 None

| 返回项 | 类型 / 何时出现 | 准确含义、示例与消费方式 |
| --- | --- | --- |
| cwd() 的非 None 返回 | str | 本次快照的工作目录，如 D:/projects/repo/app/src；不是 {'cwd':...}，直接使用字符串 |
| root() 的非 None 返回 | str | 本次快照的项目根，如 D:/projects/repo/app；不是 Git 根、内部 Agent workspace 或临时目录 |
| None | 该项未提供 | 表示当前上下文没有该目录资料；不证明会话不存在或磁盘目录不存在，不回退为 '.' / 空字符串 |

例如 cwd=repo/app/src、root=repo/app，而 repo() 可返回 repo。三者可以不同；无项目会话仍可能有 cwd、root=None。若事件只提供 session_id，两项都可能为 None。两次读取同一调用中的目录是同一事件边界的事实，不是持续跟踪外部 chdir 的实时视图。

### 4.3 错误与缺失不要混用

| 情况 | 能力函数行为 | 用户应如何处理 |
| --- | --- | --- |
| 没有 SessionContext | RuntimeError | 选择平台已绑定会话的事件接入；不手工猜会话 |
| 目录为 None | 返回 None | 显示未知或跳过需要目录的业务 |
| 目录不是 str/None | TypeError | 属于平台资料格式错误，不冒充“无项目” |
| 空字符串或非本机绝对路径 | ValueError | 不用相对路径补救为另一目录 |

经 EventBus 时，不合法上下文也可能先被 Core 拒绝。USER 中未捕获异常按原事件的故障隔离处理；原回合继续不等于本次查询成功。

## 5. 平台接入与用户使用

```text
启动：Worker 装配 → ops.build → CORE 返回 Builder
  → session_ops_build BUILTIN 在 next 返回后登记共享 session 方法
  → Core 保存能力目录；Registry 发布用户 Handler

业务：已接线的平台入口收集真实 SessionContext
  → Worker EventBus.trigger(原事件, context)
  → Core 为实际 Handler 创建当次 Ops，绑定会话和包身份
  → 用户 Handler await 本特性只读 Ops
  → Core 方法代理自动把当前 Ops 传给对应平台函数
  → 平台读取可信快照或本特性明确允许的数据源 → 直接返回值
  → 用户自行记录/展示，再 next_handler(原事件)
  → 剩余原事件 Handler → 原 CORE → 原通知任务收到回执
```

启动时准备的是共享方法目录，不执行查询。业务时创建绑定本次会话/包身份的 Ops 视图，不按事件名只创建少数方法；用户实际 await 哪个方法才执行哪个查询。这里的“Core 方法代理”是框架适配，不是新建的业务 CORE。

USER 负责自己的日志或展示；BUILTIN 负责启动时登记 Ops；原事件 CORE 处理原事件。三个查询特性均不新增专用查询 CORE、Rail、Front/Supervisor 协议。EventBus 仍只由 Worker 持有。

用户提供 manifest.yaml / extension.py，通过既有管理入口安装包并进行正常业务操作。平台负责业务触发和可信资料，用户不修改 Adapter，不创建 EventBus/Builder，不调用 notify 来“取回上下文”。安装成功只说明 Handler 已发布；尚无对应业务事件时不会自动执行查询。

返回值交给调用方，不自动缓存查询结果、写第二份状态文件或渲染 UI。示例的 logger 是用户显式写日志；其他使用方式需用户接好自己的消费逻辑。卸载移除 USER Handler，不删除平台能力或既有会话事实。


### 5.1 可复制的用户包

PR01 没有另附仓库示例目录；用户可把下面两个文件放在同一新目录并打成 ZIP。日志是显式消费结果，不代表自动前端展示。

`extension.py`：

```python
import logging

logger = logging.getLogger(__name__)

class WorkspaceInfo:
    def register(self, registrar):
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")

    async def on_usage(self, event, ops, next_handler):
        cwd = await ops.session.cwd()
        root = await ops.session.root()
        logger.info("workspace: cwd=%s root=%s", cwd, root)
        return await next_handler(event)

def create_extension():
    return WorkspaceInfo()
```

`manifest.yaml`：

```yaml
schemaVersion: 1
package:
  id: example.session-workspace-info
  version: 1.0.0
  displayName: Session Workspace Info
extensions:
  - id: workspace-reader
    runtime: backend
    module: extension.py
```

将两文件放在 ZIP 根目录，在服务可读路径使用 `/extension install "D:/extensions/session-workspace-info.zip"`。正常消息产生有效原生用量通知后，USER 日志可见 cwd/root；其他已有会话绑定事件同样可调用。原生入口会提供真实快照，但本特性不承诺所有未接线入口都有目录资料。

## 6. 验收与未决问题

2026-10-29 本轮只修改文档，不重跑历史业务/模型验收。已核对无参签名、目录读取与错误、示例注册/调用/next、完整链路和本地引用；验证记录：`D:\work_space\.codex-temp\server-session-docs-20261029\documentation-verification.json`。

<a id="5-验证设计执行记录与结论"></a>

### 6.1 原功能验证（2026-10-26，保留原结果）

| 场景 | 验收要求 | 实际结果 |

| cwd | 方法返回类型保持；内部现在派发 session.cwd。用户 Handler 新增拦截后可改变本次返回视图，真实执行目录不变。 |
| root | 方法返回类型保持；内部现在派发 session.root。改写查询值不会改变平台项目绑定或文件访问根。 |

### 5.3 可运行案例与效果

分支示例为 [examples/session_query_audit](../../examples/session_query_audit/)，统一七项示例见 [session_query_audit](../../examples/session_query_audit/)。验证复用 Token Weather 订阅的既有 context.usage.updated 通知作为入口；collect_session_parameters 返回包含七项真实查询结果的参数字典，交给用户自己的 on_parameters 业务函数。测试逐字段核对字典，并将 ui.render 设置为一旦调用就失败；不介入原 Token Weather 渲染代码。

具体例子：平台在一次真实回合后绑定 session-a 的 cwd、项目根、模型/用量观测并触发 context.usage.updated；用户 on_usage 调用 ops.session.cwd / ops.session.root，代理进入同名查询链，查询 USER 可审计，BUILTIN 计时，CORE 读取原服务；代理解包，用户消费返回参数，再 next 原用量通知。用户只安装自己的包和 Handler，不编写 Adapter 收集资料。

## 6. 验收与未决问题

| 本次实际验证 | 结果 | 范围 |
| --- | --- | --- |
| 扩展模块回归 | 632 项通过 | 生产装配、七项查询、包加载/卸载、原历史与既有扩展行为；源码使用已合入 Core bdde48a8 |
| 相关平台回归 | 140 项通过 | 原生用量入口、历史路径/写入/游标、模型选择迁移及运行配置 |
| 独立进程参数验收 | 通过，7 项返回值逐字段一致 | 真实 ZIP 包→平台 notify→context.usage.updated→七个 Ops/同名查询 Event→返回参数→卸载/释放 |
| 不进入渲染链 | ui.render 调用 0 次 | 验收中 render 一旦被调用即失败；原 Token Weather 渲染文件保持原样 |

专项用例覆盖默认结果、类型/缺失语义、拦截/短路/拒绝、故障隔离、绑定身份、并发/递归/取消、真实包加载与卸载；关联平台用例仍覆盖真实 Git/JSONL 和原存储规则。模型查询验证绑定快照与保存信息，不据此宣称执行了新的模型 API 请求。未执行的浏览器/跨进程用户操作不列为通过。

## 7. 参考资料

结果为 225 项通过、11 项失败。失败集中在原有路径脱敏、路径分隔符及 Windows 大小写比较；在未修改的 `9e9539dde` 基线上用相同 Core 和解释器复跑两个失败文件，得到 45 项通过、相同的 11 项失败，失败用例集合完全一致。本次未修订这些外围行为，也不将整轮回归表述为全绿。

独立进程验证使用隔离配置目录和生产 `install_extensions()`，经真实管理门面加载临时后端包，订阅现有用量事件并调用两个 Ops。两个项目会话分别得到自己的子目录与项目根；无项目会话保留 cwd、root 为 None；缺少路径的旧发布调用两个值均为 None。卸载后用户 Handler 不再执行；停机、日志关闭与临时目录清理通过，进程退出码为 0。此验证未使用模型和网络传输，不等同于真实聊天或浏览器联调。

已核对 `handler_ops/` 归属、生产 bootstrap 接线、唯一一次 Ops Build、公开导入、文档编号和引用；按仓库约束未运行格式化或静态检查工具。当前进度和验收依据统一维护在关联开发计划中。

### 6.2 当前边界

未提供可信目录的入口需由该平台入口维护者接线；注册 USER Handler 本身不自动收集目录。目录读取不代表目录存在、授权到所有文件、完整恢复任务或浏览器已展示。原历史验证的替身/真实进程/网络边界保留原记录，本轮不扩大解释。

## 7. 参考资料

- [S_03](../specs/S_03_session-workspace-ops.md)、[session_ops.py](../../handler_ops/session_ops.py)、[通知定义与平台触发](../../event_types/context_usage.py)。
- [PR02 主模型](F_14_session-model-ops.md)、[PR03 Git 工作树](F_15_session-repo-ops.md)：目录身份与模型/Git 事实不同。
- [内置事件接入说明](../designs/builtin-event-development-guide.md)、[Claude 固定版本声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)。

- [Claude 会话方法](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L2342-L2447)
- [Claude OpEventOf 与拦截规则](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L5529-L5624)
- [S_16 查询公共协议](../specs/S_16_session-query-events.md)
- [F_27 统一实施记录](F_27_session-query-events.md)
- [内置事件开发指南](../designs/builtin-event-development-guide.md)
