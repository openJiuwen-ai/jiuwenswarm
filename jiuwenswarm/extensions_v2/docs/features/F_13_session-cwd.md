# session.cwd / ops.session.cwd 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_13 / session-cwd |
| 建档日期 | 2026-10-26 |
| 后续修订 | 2026-10-31，拆分独立查询特性、按事件命名；本次以 session.append 的七章结构补齐字段、接入实例与函数链，契约不变；2026-11-02，按原 session.append 对象示例、字段表及业务 Handler 写法统一说明，契约不变；同日补充查询结果的资料来源、读取函数、复用与新增边界及缺失处理，契约不变 |
| 关联计划 | [EXTV2-22 / 七项只读查询事件化](../designs/extensions-v2-development-plan.md#extv2-22-七项只读查询事件化) |
| 主责规约 | [S_16 查询公共协议](../specs/S_16_session-query-events.md)、[S_03](../specs/S_03_session-workspace-ops.md) |

## 1. 范围、状态与依据

最近修订 2026-11-02。本文仅维护 `ops.session.cwd()` 与同名查询事件 `session.cwd` 的使用说明、接入和验证边界；一个方法与一个查询事件组成一项特性。开发状态、负责人和交付统一见[开发计划 EXTV2-22](../designs/extensions-v2-development-plan.md#extv2-22-七项只读查询事件化)，交付 [PR !100](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/100)。

公共协议主责为 [S_16](../specs/S_16_session-query-events.md)，专项字段主责为 [S_03_session-workspace-ops.md](../specs/S_03_session-workspace-ops.md)；下文表格解释怎样填写、怎样消费，不另立契约。共享实现和既有验收记录见[查询公共机制与七项特性索引](../designs/session-query-events.md)。

2026-10-31 按单个 Ops / Event 拆分特性文档；原合篇只作为文档组织历史，不代表仍需一起调用。此次拆分没有改变运行时契约或实现。

## 2. 能力说明与 Claude 对照

`session.cwd` 是这一次查询调用的拦截事件。方法代理验证参数和已绑定会话后触发它；查询 Handler 接收下述参数，可继续默认处理、增加前后操作、返回合法替代视图或明确拒绝。默认 CORE 读取平台事实，结果返回原方法调用者；它不表示状态发生变化，也不自动写历史或显示到界面。

`await ops.session.cwd()`：查询本次 Handler 所绑定会话的执行工作目录，适合生成工作区摘要或对读取行为做审计。它不切换目录、不检查目录是否仍存在，也不读取 Worker 的进程当前目录。

| 项 | 本次实现 | 与固定 Claude 定义的关系 |
| --- | --- | --- |
| 事件输入 | 无业务选项，Event 输入为空映射 {} | 以同名查询 OpEvent 为扩展点；会话身份独立绑定，不作为可改写参数 |
| 默认数据来源 | SessionContext.cwd；经本机绝对路径校验。 | 使用本地真实平台服务或绑定快照，不伪造 Claude 私有宿主数据 |
| 方法公开返回 | str 或 None | Claude 返回 string；本地在平台没有目录资料时保留 None，不伪造当前进程目录。 |
| 改写与拒绝 | 可 next、短路合法 value、返回合法 deny | 只影响本次查询结果，不修改平台会话事实或授权范围 |

对照依据是 Claude Code Mods 2.1.277 的固定公开类型声明，提交 `80c6959d28c4c30ecd14fc1cdf172f19d9adad81`；不是运行 Claude 私有宿主的结果。

## 3. 事件契约

### 3.1 触发前后与粒度

```text
平台业务入口先收集可信会话资料，触发外层事件并传入 SessionContext
  → Core 为实际用户 Handler 绑定本次 Ops
  → 用户 await ops.session.cwd()
  → 方法代理读取 ops.session_context，规范化参数，校验会话与重入
  → await Worker bus.trigger(Event("session.cwd", arguments), context)
  → 查询 USER Handler：可 next、加工合法返回视图或明确 deny
  → await next_handler(event)
  → 共享查询 BUILTIN 开始计时并继续
  → CORE 读取 SessionContext.cwd，校验为宿主绝对路径或 None
  → {value: 结果} 经 BUILTIN／USER 返回；或 USER 返回 {deny: 原因}
  → 代理校验、复制并解包；原 Ops 调用者得到值或明确错误
  → 用户消费结果，再 next 原外层事件
```

一次 Ops 调用发起一次本查询；前置校验未通过时不触发。同一外层 Handler 调用两次会读取两次；只有订阅、没有调用时不会自动查询。查询是等待期间另起的一条链，返回后原 Handler 从 await 后继续。每个匹配 USER 只有调用 next 才进入后续 Handler；合法短路可不进入默认 CORE。

### 3.2 输入：每个字段表达什么、由谁填写

实际类型是 `openjiuwen.core.events.Event`，event_input 为查询映射。下面展示 Ops 代理生成的对象形态；普通调用者使用第 4.1 节的方法即可。平台会话上下文通过 trigger 的独立参数传递。

```python
from openjiuwen.core.events import Event

query_event = Event("session.cwd", {})
```

| 字段 | 类型 / 是否总有 | 准确含义 | 应该填写什么、示例 | Handler 权限 |
| --- | --- | --- | --- | --- |
| event_type | str，必有；固定为 session.cwd | 本次查询的事件类型，Bus 据此选择处理链 | **Ops 代理填写**；示例为 "session.cwd"，不填入参数映射 | 只读；保持当前类型 |
| event_input | 只读 Mapping，必有 | 本次查询选项，不包含目标会话、目录或模型快照 | **Ops 代理填写**；{} | 只能保持空映射；没有可改写的业务选项 |

本事件没有额外业务字段，event_input 必须为 {}。用户调用是 `await ops.session.cwd()`，括号留空；不能传 session_id、路径、空字典或其他选项。

#### 随调用绑定的会话资料

这些资料由平台业务入口收集，Core 绑定到本次 Handler 的 Ops。用户读取 `ops.session_context` 即可；它是只读属性，不需要 await，也不负责采集或组装上下文。查询参数和返回视图都不能替换这份可信资料。

| 字段 | 类型 / 是否必有 | 含义与应该填写什么 |
| --- | --- | --- |
| ops.session_context | Core 只读属性，查询需要非 None | 本次 Handler 的可信会话资料入口。平台把上下文随外层 Event 交给 Bus，Core 绑定到实际 Handler 的 Ops。填写／示例：用户不用填，也不用 await；直接读取 ops.session_context。用途：代理取出这份资料，随查询 Event 的独立 context 参数继续传递。缺失或错填：缺少上下文时查询报错；不要把用户输入 payload 包装成平台可信上下文 |
| SessionContext.session_id | 平台验证过的非空 str，必需 | 本次绑定的是哪个会话。平台业务入口提供；用户普通调用不填写。填写／示例：平台例如绑定 session-a；查询方法和 Event 输入里都不再填 session_id。用途：读取相应会话的历史、保存模型或用量服务；同时参与隔离和循环保护。缺失或错填：缺失/非法不能查询；返回值或 Event 输入不能把目标改成另一个会话 |
| SessionContext.cwd | str 或 None | 本次执行目录的绑定事实。平台收集当前执行目录。填写／示例：平台按宿主系统提供绝对路径；用户不用补填。用途：默认 CORE 将它校验后作为 cwd 返回。缺失或错填：没有资料保留 None；不会读进程 cwd 替代 |
| current_query_origin().package | str 或 None；helper 本身也可返回 None | 谁发起了本次查询，而不是当前查询 Hook 属于哪个包。Server 分派器从原 Ops 的包身份记录；不是用户传参。填写／示例：先 origin = current_query_origin()，非 None 时再读 origin.package。用途：用于用户自己的审计和来源判断；它不是 Core next.origin 或授权证明。缺失或错填：直接宿主触发而没有 Ops 调用域时 helper 可为 None；不要无判空访问 .package |

Bus 保留会话身份并生成规范化只读副本；外层和查询 Handler 不要求持有同一个 Python 对象。原查询包身份由分派器记录，不能用当前 Hook 所属包冒充原调用者，也不能把来源审计当作授权证明。

### 3.3 输出：默认值、替代结果与拒绝

默认 CORE 返回恰含 value 的映射；示例为 Python 对象，None/True/False 在 JSON 中分别对应 null/true/false：

```python
{'value': 'D:/projects/demo/src'}
```

| 字段 | 类型 / 返回时机 | 含义与调用方怎样使用 |
| --- | --- | --- |
| value | str 或 None；成功时唯一顶层键 | 本次 Handler 绑定的执行工作目录，可以是项目内部的子目录。默认 CORE 读 SessionContext.cwd；查询 USER 可提供合法替代视图。填写／示例：Windows 如 D:/projects/demo/src；Linux 如 /home/me/demo/src；没有资料填 None。用途：给工作区摘要加目录标签；用户自己的业务可据此选择后续操作。返回值本身不会切换目录。缺失或错填：None 表示平台没提供目录资料；不要填空字符串、相对路径、Path 对象或 {cwd: ...}。路径必须符合宿主操作系统的绝对路径语法；不会检查目录仍存在。Ops 校验、独立复制并解包；普通调用者拿到 value 本身，查询 Handler 从 next 得到完整封装。 |
| deny | 非空 str；拒绝时唯一顶层键 | USER 如需明确拒绝，返回 {"deny":"QUERY_DISABLED"}。原字符串最多 512 字符，去首尾空白后须非空；不得与 value 同时出现。代理抛 SessionQueryDenied，code=DENIED、reason 保留原原因。抛普通异常不等于明确拒绝。 |

路径例子按宿主系统解释：Windows 用带盘符的绝对路径，Linux/macOS 用 / 开头的绝对路径。用户修改的是查询返回视图；平台不会据此更换实际目录、项目绑定、Git 查询目标或授权根。

成功／拒绝只能保留表中对应的一个顶层键，不增加其他键，也不能直接返回裸值。 未知可以返回 {"value": None}，不能直接 return None。

正常继续返回 `await next_handler(event)` 的完整结果，不要再包一层 value。USER 可在 next 前返回合法 value 短路，或在 next 后加工自己的返回副本；真实平台资料、授权目标及后续默认读取不随之改变。

USER／BUILTIN 的普通异常或非法输出沿用 Core 故障隔离，可能继续原有效链或保留真实下游结果；不能靠抛异常或错填可靠拒绝。默认 CORE／框架读取故障和取消沿等待关系传播。多次 next 可能重复读取，不承诺一次调用内结果缓存。

### 3.4 注册、筛选、改写示例

本例要做的事情是：**把执行目录统一为正斜杠格式，供自己的工作区摘要使用**。下面是扩展入口模块 `extension.py`；由已有 manifest 指定该模块并通过管理入口安装即可，包格式参照 [session_query_audit](../../examples/session_query_audit/manifest.yaml)。

```python
from pathlib import Path

class Extension:
    def __init__(self):
        self.parameters = {}

    def register(self, registrar):
        registrar.register_event_handler(
            "session.cwd", self.format_directory, {}, "user",
        )
        registrar.register_event_handler(
            "context.usage.updated", self.on_usage, {}, "user",
        )

    async def format_directory(self, event, ops, next_handler):
        result = await next_handler(event)
        if "deny" in result or result["value"] is None:
            return result
        # Format only the returned view; do not change the platform directory.
        return {"value": Path(result["value"]).as_posix()}

    async def on_usage(self, event, ops, next_handler):
        session_id = ops.session_context.session_id
        value = await ops.session.cwd()
        self.parameters[session_id] = {"cwd": value}
        return await next_handler(event)


def create_extension():
    return Extension()
```

平台绑定 `D:\projects\demo\src` 时，默认 CORE 读到该绝对目录，查询 USER 返回 `D:/projects/demo/src`。`self.parameters[session_id]["cwd"]` 保存格式化后的字符串；Linux/macOS 示例可用 `/home/me/demo/src`。未知仍为 None。格式化只影响返回视图，实际工作目录和其他查询的可信 cwd 不变。

Matcher 为等值条件合取，空字典匹配该事件全部合法输入。本事件输入为空映射，没有会话 ID 或输出字段可用作输入 matcher；用户规则可读取绑定上下文或默认结果。 查询 Handler 中的 next 继续当前 `session.cwd` 链；在其中再次调用同名 Ops 会报 QUERY_REENTRANT。

安装只发布订阅。下一次平台真实发布 `context.usage.updated` 并绑定会话时，on_usage 才调用 Ops、等待查询链、保存本扩展的 parameters，再 next 原外层事件。这里不要求 UI 渲染；`self.parameters[session_id]` 是本例可观察的消费结果，属于用户扩展自己的内存状态。其他由平台绑定了所需上下文的事件也可调用同一 Ops，不要求用户先调用 notify 或修改 Adapter。

## 4. 方法契约

### 4.1 ops.session.cwd()

推荐调用（写在已由平台绑定 SessionContext 的业务 Handler 中）：

```python
cwd = await ops.session.cwd()
label = "目录未知" if cwd is None else cwd
```

| 输入字段 | 必填 / 默认 | 应填写的内容、范围及示例 |
| --- | --- | --- |
| 方法参数 | 无参数 | 查询目标由当前 Ops 已绑定的会话上下文确定。普通调用者不填写参数。填写／示例：await ops.session.cwd()。用途：直接读取这次会话的视图；不需要另填会话 ID 或路径。缺失或错填：括号留空；不要调用 ops.session.cwd({})，也不要传 None/session_id/cwd；空 {} 仅由代理生成在 Event 输入中 |

本方法没有用户参数，调用括号留空；`ops.session.cwd({})` 也非法。Core 自动注入的当前 Ops 不由用户填写。

成功返回 **str 或 None**，直接返回值及每个字段的含义见第 3.3 节。代理把方法参数规范化为 Event 输入，从当前 Ops 取得已绑定上下文，另起 `session.cwd` 查询；事件的 {value: 结果} 经校验、复制后解包为方法返回，{deny: 原因} 转为 SessionQueryDenied。目标只来自 `ops.session_context.session_id` 及所需可信资料，不从用户参数或返回视图选择另一会话。

主要失败：没有 SessionContext 抛 RuntimeError；方法／事件参数非法报 TypeError，非法会话 ID 沿平台会话服务原契约拒绝；显式拒绝报 SessionQueryDenied（code=DENIED）；活跃同名或间接循环报 QUERY_REENTRANT，嵌套深度超过 16 报 QUERY_DEPTH_EXCEEDED。读平台服务的原始错误继续传播，取消保持 CancelledError。 用户可在调用 Ops 处捕获错误；未捕获时仍受外层事件自身隔离规则约束，不能把外层回合继续当成查询成功。

### 4.2 旧 Handler / 返回值迁移

方法返回类型保持；内部现在派发 session.cwd。用户 Handler 新增拦截后可改变本次返回视图，真实执行目录不变。

本次文档结构与说明调整不改变上述兼容边界。执行目录、模型、Git、历史和用量事实仍由平台服务维护；对返回视图的改写不成为另一项平台操作的可信输入。

| 旧调用 / 容易混淆的用法 | 现在怎样写 | 对使用者的影响 |
| --- | --- | --- |
| 原方法直接读取绑定 cwd | 调用仍为 await ops.session.cwd()，内部先触发 session.cwd | 返回仍为绝对路径字符串或 None；用户现在可以订阅查询 Hook |
| 修改返回路径后期待平台 chdir | 返回值只作为本次查询视图 | 实际执行目录及其他查询的可信 cwd 不变 |

## 5. 接入与兼容方案

<a id="51-启动登记上下文绑定与-core-分工"></a>

### 5.1 启动登记、上下文绑定与 CORE 分工

event_types/session_queries.py 定义查询映射、Matcher、Validator 并注册同名事件及唯一 CORE；handler_ops/session_queries.py 创建封闭代理；query_dispatch.py 负责上下文、来源、重入保护、trigger 和结果解包；event_handlers/session_queries.py 提供共享查询 BUILTIN 与 CORE；实际读取由 server/runtime/session/query_views.py 及所属平台服务负责。

启动阶段，Registry.init 触发一次 ops.build：其 CORE 创建 Builder，平台 BUILTIN 从 next 取得同一 Builder，登记七个 session 查询代理，Bus 保存方法目录。业务阶段，Core 在实际进入每个 Handler 前，用该目录、可信包身份和本次 SessionContext 创建 Ops；方法访问器自动注入当前 Ops。Builder 保存方法定义，查询结果在每次调用时产生。

现成平台通知入口的调用形态如下（**平台代码示意**；变量来自真实回合，用户扩展不负责采集）：

```python
notify_context_usage(
    update,
    cwd=cwd,
    project_root=project_root,
    model_info=model_info,
    context_usage=context_usage,
)
```

notify_context_usage 把会话 ID、可信目录与可得快照整理为 SessionContext，调度 _dispatch；后者调用 `bus.trigger(Event("context.usage.updated", update), context)`。用户 on_usage 使用 Core 已绑定到 Ops 的同一份会话资料。这个通知仅是现成示例入口；平台其他业务已绑定了有效上下文时，同样可以调用查询 Ops，无需先执行用量通知。

业务查询的默认 CORE 落点：**CORE 读取 SessionContext.cwd，校验为宿主绝对路径或 None**。CORE 不回调公共同名 Ops，以免递归；查询 USER 执行本包规则，共享 BUILTIN 计时、记录原调用来源并复制结果，然后反向返回。它与启动时创建 Builder 的 ops.build CORE 是两种不同职责。

完整业务过程：平台发布外层事件并绑定上下文 → on_usage 调用 `ops.session.cwd()` → 查询代理触发同名事件 → `format_directory` 介入 → next 进入 BUILTIN／默认 CORE → 返回查询结果并应用本例规则 → 代理解包 → 用户保存 parameters → next 继续原外层事件。具体可观察效果见第 3.4 节；第 4.1 节是独立调用法，不要求安装其他六项示例。

默认查询不写平台业务数据、不发模型请求、不自动渲染，也没有全局结果缓存。对返回视图的加工不会成为其他平台操作的可信输入。用户包卸载／重载撤销自己的 Hook，不撤销平台事件、CORE 或共享 Ops；用户自有内存参数与缓存的保留由用户扩展管理。

七项联合示例见 [session_query_audit](../../examples/session_query_audit/)；其中 collect_session_parameters(ops) 是普通用户辅助函数，分别调用七个 Ops 后返回参数字典。它与本文单项业务规则的示例分别维护。共享机制、来源及取消边界见[公共设计第 5 节](../designs/session-query-events.md#5-接入与兼容方案)。

源码位置：[事件与校验](../../event_types/session_queries.py)、[Ops 登记](../../handler_ops/session_ops.py)、[查询代理](../../handler_ops/session_queries.py)、[分派](../../query_dispatch.py)、[查询 CORE／BUILTIN](../../event_handlers/session_queries.py)、[平台读取](../../../server/runtime/session/query_views.py)、[现有平台通知入口](../../event_types/context_usage.py)。

### 5.2 查询结果如何获取

以下说明查询 USER 正常 `await next_handler(event)` 后，默认 CORE 如何取值。公开 `ops.session.cwd()` 仍先经过同名 `session.cwd` Event；`ops.session_context` 是 Core 绑定的只读属性，不负责采集资料。USER 短路返回合法 value 时不会执行下列默认读取；USER 在 next 返回后加工 value 时，Ops 拿到的是加工后的视图。

```text
SessionContext.cwd
  → read_session_query("session.cwd", {}, context)
  → _path(context.cwd, "session.cwd")
  → Event 返回 {"value": 绝对路径或 None} → Ops 返回该路径或 None
```

| 阶段 | 具体从哪里获取 | 如何形成当前结果 |
| --- | --- | --- |
| 平台资料 | 现成回合入口在 interface_deep.py 调用 Core 的 `get_cwd()`，把当前执行目录作为 `cwd` 传给 `notify_context_usage` | 通知桥接构造 `SessionContext(cwd=cwd, ...)`；其他平台入口也可提供自己的可信 cwd，用户不需要再填写路径 |
| 查询身份 | 代理从 `ops.session_context` 取得绑定上下文，校验其中的 `session_id` | 会话 ID 用于确认当前查询所属会话；本项不按 ID 再读会话目录，也不取 Worker 的进程 cwd |
| 默认 CORE | [read_session_query / query_views.py](../../../server/runtime/session/query_views.py) 的 cwd 分支只调用 `_path(context.cwd, event_type)` | 校验宿主绝对路径的形式后原样返回；不扫描磁盘、不验证目录存在、不执行 chdir |
| 返回 | `session_query_core` 包成 `{"value": cwd}`；代理校验并解包 | `cwd="D:/projects/demo/src"` 时，`await ops.session.cwd()` 得到同一个字符串；没有额外结果字典 |

**复用与新增：** 执行目录来源复用平台已有的 Core cwd 上下文。PR-01 新增了扩展侧的工作区读取适配；七项事件化改造再增加 `session.cwd` 查询 Hook 和代理，并由 query_views.py 承接默认读取。不是重新实现一套目录发现机制。

**缺失与失败：** 绑定 `cwd=None` 时返回 `None`；空字符串、相对路径报 `ValueError`，非字符串且非 None 报 `TypeError`。即使 session_id 有效，也不会自动改读磁盘、项目根或进程目录。

共享查询 BUILTIN 只计时、记录调用来源并复制结果，具体读数由默认 CORE 委派平台服务完成；CORE 将结果包成 `{"value": ...}`，Ops 代理再校验、复制、解包。结果返回调用它的用户 Handler，是否保存为自有参数由用户决定，不自动写历史或进入 UI。

## 6. 验收与未决问题

### 6.1 本特性的独立验收场景

| 场景 ID | 输入与前置 | 操作 | 预期结果 |
| --- | --- | --- | --- |
| F_13-V-01 | 已绑定执行目录 D:/projects/demo/src | 调用 cwd() | 返回该绝对路径；不会 chdir，也不验证磁盘存在 |
| F_13-V-02 | 有效会话但 cwd=None | 调用 cwd() | 返回 None，不伪造进程当前目录 |
| F_13-V-03 | 安装仅拦截 session.cwd 的用户包 | 分别 next、短路 value、显式 deny | 默认值 / 合法替代值 / SessionQueryDenied 分别可观测 |
| F_13-V-04 | 不同会话绑定不同上下文；或在 Hook 中同名递归 | 分别调用本方法 | 会话隔离；活跃同名或间接循环报 QUERY_REENTRANT，不能无限递归 |
| F_13-V-05 | 读取失败、Hook 异常或查询取消 | 分别构造这些分支 | 按第 3.3 / 4.2 节区分隔离、明确拒绝、平台错误与取消，不把失败当成功空值 |

对应事件化用例位于 [test_session_query_events.py](../../../../tests/unit_tests/extensions_v2/test_session_query_events.py)：包含七项默认调用、各项短路 / 拒绝、非法参数、查询顺序、隔离、循环 / 取消及实际用户包装载卸载。每个场景只验证本方法承诺，不把其他六项也作为调用前置。

### 6.2 已有实施证据与本次文档调整

实现验证基于本 PR 实现提交 `f313f513ee9b`、已合入 Server 基线 `c6a07f909` 与 Core `bdde48a8`，Python 3.11。扩展 632 项、相关平台 140 项及独立进程 ZIP / notify / 七项参数返回验证的实际范围、命令和限制统一保留在[公共设计第 6 节](../designs/session-query-events.md#6-验收与未决问题)。这属于查询事件化实现的既有证据；这些数量属于实现阶段历史运行。本次按 session.append 对齐文档的结构和说明，运行代码不变；只核对字段、示例、源码链路、链接与差异，不声称重新执行业务或真实模型测试。

Claude 返回 string；本地在平台没有目录资料时保留 None，不伪造当前进程目录。 该限制属于已知支持边界；此轮未增加新的运行时待决项。未执行 Claude 私有宿主、新模型 API、浏览器或 Front/Supervisor 跨进程验收，不能由文档完整推断这些验收已完成。

2026-11-02 文档整理：以 2026-10-31 的 session.append 原件为参照，统一输入对象示例、字段表列式、输出嵌套字段、可运行 USER 业务案例、Ops 参数与迁移说明。运行代码、公共字段与既有历史验收证据保持；本轮仅验证文档结构、示例行为、类型／字段、链接及差异，不作为业务、模型或浏览器测试的新结果。

## 7. 参考资料

- [专项字段规约 S_03_session-workspace-ops.md](../specs/S_03_session-workspace-ops.md)
- [S_16 查询公共协议](../specs/S_16_session-query-events.md)
- [七项独立特性索引与共享实施记录](../designs/session-query-events.md)
- [Claude 会话方法](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L2342-L2447)
- [Claude 同名 OpEventOf 查询事件](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L5529-L5624)
- [内置事件开发指南](../designs/builtin-event-development-guide.md)
