# server.run_command 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_30 / server-run-command |
| 归档日期 | 2026-11-04 |
| 后续修订 | 2026-11-04：按 session.append 的字段表写法区分含义、填写方式和权限，补充 query 识别及自定义命令接管规则 |
| 交付 PR | [!119](https://gitcode.com/huenrui_/jiuwenswarm/pull/119) |
| 关联计划 | [EXTV2-29](../designs/extensions-v2-development-plan.md#extv2-29-server-斜杠命令执行扩展) |
| 主责规约 | [S_17](../specs/S_17_server-run-command.md) |

## 1. 范围、状态与依据

本项目保留 `server.run_command` 名称，对照 Claude Code Mods 固定提交 `bf2323b3021c9d3db7a31e1e7d61068c3f704497`、类型文件标注版本 2.1.277 的 `command.run`、`CommandRunInput`、`CommandRunResult`、`CommandPresentation` 和 `PromptOrigin`。它处理 `/name args` 这种斜杠命令；系统进程执行属于 Claude 的 process.run。

本次实现事件、唯一 CORE、可信调用封装和 Worker DeepAdapter 原生文本命令入口。没有新增插件 Ops 方法。目标分支已合入 authorize PR !114 的 F_29 / S_16，本文沿用已登记的 F_30 / S_17。合并冲突时保留两个事件的独立注册。

## 2. 能力说明与 Claude 对照

`server.run_command` 在 Worker 要执行一条原生文本斜杠命令时触发。平台固定命令名、来源、会话和展示事实，USER Handler 可以改写 args、继续原生命令、直接接管返回文字和模型备注，或在 next 返回后加工文字。默认 CORE 调用原有命令处理函数，平台在事件链结束后消费最终结果；事件本身不发送第二条模型 query。

| 对照项 | Claude command.run | 本次实现与使用影响 |
| --- | --- | --- |
| 名称 | command.run | 按项目要求命名 server.run_command |
| 输入 | command / args / origin / presentation | 同名字段；Python 使用冻结 dataclass，presentation 保留 isFullscreen 的原拼写 |
| 输入改写 | args 可改，命令与来源固定；省略 presentation 时继承 | 对齐；在 Handler 内构造新输入时 presentation=None 会补成本次平台值 |
| 返回 | 可选 text / context / ref | 同名字段；Python 的 context 使用 tuple[str, ...] 对应字符串数组 |
| 接管 | 不调用 next，返回自己的 text/context | 对齐，原生命令执行次数为 0 |
| 加工结果 | next 后替换打印文字，保留已有 context | 对齐；替换 text 时返回一个不带 ref 的新结果；新增备注放在原备注之后 |
| 原生结果引用 | CORE 的 ref 指向宿主本次执行结果 | 对齐语义；数值仅在本次调用有效，不是会话 ID、消息 ID 或公共查询键 |
| 可观察效果 | 打印文字，context 为模型可见的隐藏用户消息 | 文字复用现有 unary/stream 返回；备注写入 ContextEngine 并保存 Session，不进入 ui.render。短路响应沿用本项目 source=extension，不新增 Claude 的插件署名布局 |
| 视口事实 | 宿主提供 isFullscreen/columns | Worker 没有本机终端测量时使用 false / 80；有测量结果的宿主可显式传入 |
| 触发覆盖 | Claude 引擎自己的斜杠命令路径 | 本次接 Worker DeepAdapter 文本命令；Gateway 消费的命令和单独 command.* RPC 不会自动经过此事件 |
| 方法与命令目录 | 另有 $.command.run/register/list | 本次范围为 Event，没有新增这些 Ops，也没有增加前端补全菜单 |

## 3. 事件契约

### 3.1 触发点、前后流程与粒度

当前真实调用点是 `JiuWenSwarmDeepAdapter._handle_slash_command()`。它由原来的 unary/stream 请求处理调用；每次进入该位置的一条 `/name args` 触发一次。原先的命令代码保存在 `_handle_native_slash_command()`，只有事件链继续到 CORE 时才执行。

**query 如何进入事件链：**请求先通过原有的实例、会话和来源检查，再由 [run_adapter_slash_command](../../../server/runtime/commands/adapter_commands.py) 统一识别。query 必须是字符串；忽略开头空白后，整条输入须符合 `/([A-Za-z0-9_-]+)(?:\s+([\s\S]*))?`。命令名只接受 ASCII 字母、数字、下划线和短横线，至少一个字符；后面可以没有参数，或用空白分隔参数。解析后去掉 `/`，命令名转为小写，余下内容作为一个 args 字符串，不按 shell 规则拆词。例如 `/PROJECT_status demo` 得到 command="project_status"、args="demo"；`请执行 /project_status`、`/` 和 `/项目检查` 不符合该格式，继续原处理，不触发本事件。

**自定义命令如何生效：**新增的是统一解析和进入扩展事件链的适配；开头空白处理延续原有入口习惯。命令不要求已被原生平台实现。`/project_status demo` 可由匹配的 USER Handler 直接返回合法结果，完成用户自己的命令；如果它继续调用 next，最终原生平台仍不认识，则返回 COMMAND_UNKNOWN。触发资格仍受下表中的原有路由限制，不能用自定义命令绕过来源检查。

| 输入场景 | 当前行为 |
| --- | --- |
| Worker 的 /evolve、/evolve_list、/evolve_simplify、/evolve_rebuild、/evolve_rollback | 先经过事件，再执行已有演进命令代码 |
| TUI 原生文本 /goal 的即时处理分支 | 在它进入原生文本命令方法时经过事件；原本延迟至 attach 后执行的 set/resume 分支继续遵守原调度 |
| 用户包自己处理 /project_status | 同样经过事件；USER 返回结果，原生命令不执行 |
| 普通文字、结构化 A2UI 输入 | 继续原处理，不触发本事件 |
| Web 的普通聊天文字 /goal ... | 保留原有“普通文字”策略，不触发本事件 |
| 跨会话内部消息 | 原流程已跳过文本命令解析，本次保持该来源边界 |
| Gateway/前端本地消费的命令、command.compact 等独立 RPC | 当前没有接本事件；需要所属入口单独适配，不能把本次 Worker 验证扩大为这些入口已支持 |
| Extensions V2 未装配 | 直接执行原生文本处理函数 |

```text
原 chat.send unary / stream → 原生会话/实例路由及原入口检查
  → _handle_slash_command(query, session_id, mode, channel_id)
  → run_adapter_slash_command：解析命令，绑定可信 SessionContext
  → dispatch_server_run_command：保存原生执行函数、Task 与私有结果
  → await bus.trigger(Event("server.run_command", input), context)
  → Core 为本次匹配 USER Handler 绑定已有 Ops、包身份和同一会话
  → USER：读字段、改 args，或直接返回 text/context
  → await next_handler(原事件或带新 args 的事件)
  → 其余 USER → 可选 BUILTIN → server_run_command_core
  → 校验当前 Task / 会话 / 保护字段
  → frame.execute_once(args)：原生命令只执行一次
  → _handle_native_slash_command → 原演进/Goal 命令实现
  ← NativeCommandResult：完整原生 payload 留在平台，公开 text/context/ref
  ← CORE → BUILTIN 的 next 之后 → USER 的 next 之后
  → 平台校验最终结果；继承备注、保留原生副作用
  → append_command_context：逐条 UserMessage → save_contexts → Session.commit
  → 原 unary/stream 代码消费命令结果、审批块或 follow-up
```

同层匹配和故障处理沿用 EventBus。改写 args 不重新匹配 Handler。matcher 复用本项目的精确值/点路径子集匹配，本次没有增加 Claude 的正则和数组 matcher。next 继续当前命令链；在 Handler 中查询 `ops.session.cwd()` 会另外触发 session.cwd 查询链，两个事件不是同一个业务动作。

### 3.2 输入类型与字段

实际导入位置为 `jiuwenswarm.extensions_v2.event_types.server_run_command`，定义见 [server_run_command.py](../../event_types/server_run_command.py)。Handler 通过 event.event_input 读取 ServerRunCommandInput 对象，不接收任意 JSON 字典变体。以 Web 原生文本入口收到 `/project_status demo` 为例，平台构造的公开输入如下：

```python
ServerRunCommandInput(
    command="project_status",
    args="demo",
    origin=CommandOrigin(kind="bridge"),
    presentation=CommandPresentation(isFullscreen=False, columns=80),
)
```

第一次触发时四个字段都由宿主提供。presentation 的 None 仅用于 Handler 改写时表达继承，后续 Handler 仍会得到完整的平台展示事实。

| 字段 | 类型 / 是否必有 | 准确含义 | 应该填写什么、示例 | Handler 权限 |
| --- | --- | --- | --- | --- |
| command | str，必有 | 不带 / 的规范命令名，用于 matcher 和选择原生处理路径 | **平台填写**；从 query 拆出并转为小写，例如 /PROJECT_status demo 对应 project_status。宿主入口负责别名解析，用户不在此填另一条命令 | 只读，不得改成另一条命令 |
| args | str，必有；无参数时为 "" | 命令名之后的整段参数文字，不是整个 /command 行，也不是 JSON 参数字典 | **平台先填写**；例如 /project_status demo 对应 "demo"。名称后的分隔空白不算参数，参数内部和末尾空白保留；默认不进行 shell 拆词。用户可填写修订后的参数字符串 | 可通过 replace(input, args="demo") 改写，允许空字符串 |
| origin | CommandOrigin，必有 | 平台确认的输入来源分类，用于按来源制定策略，不是用户提交的身份声明 | **平台填写**；例如 CommandOrigin(kind="bridge") 表示当前 Web 桥接输入。用户通过 input.origin 读取 | 只读，须保留原对象的值 |
| origin.kind | str，必有 | 下表列出的来源字面值；不代表会话 ID，也不代表权限等级 | **平台填写**；当前 Web 原生文本入口为 "bridge"，SDK 通道为 "sdk"。用户可写 matcher={"origin.kind": "bridge"} 筛选 | 只读，可用于点路径 matcher |
| origin.name | str 或 None；默认 None | kind=plugin 时的真实发起插件名称；不是任意用户昵称或目标会话 | **平台填写**；只有可信插件来源才能提供非空名称，其余来源填 None。当前原生文本入口不会构造 plugin 来源，因此为 None | 只读 |
| origin.server | str 或 None；默认 None | kind=channel 时的渠道服务名称，不是 HTTP URL 或 MCP 凭据 | **平台填写**；如 CommandOrigin(kind="channel", server="feishu")；其他来源填 None | 只读 |
| presentation | CommandPresentation，进入链时必有；构造器默认 None | 运行时输出的布局与宽度事实，供扩展组织文字；不代表前端已经挂载 | **平台首次填写完整对象**，例如 CommandPresentation(isFullscreen=False, columns=80)。Handler 重建输入时可省略或传 None，表示继承本次平台值 | 有效值只读；省略/None 不会清除原值 |
| presentation.isFullscreen | bool，默认 False | 输出是否使用终端全屏布局 | **平台填写**；Worker 没有终端布局事实时使用 False。不能只根据渠道为 Web 就填写 True | 只读 |
| presentation.columns | int，默认 80，必须大于 0 | 终端字符单元宽度，供扩展决定文字排版；不是浏览器像素宽度 | **平台填写**；有实际测量时可为 120，没有终端测量时使用 80 | 只读 |

`CommandOrigin` 对照 PromptOrigin 全部来源变体，字段组合需要符合下表。仅声明一种来源不会让平台自动产生那种来源。

| kind | 含义 / 附加字段 |
| --- | --- |
| composer | 宿主能够证明的用户终端操作；不能只凭 channel_id=tui 推定 |
| bridge | Web/远程桥接输入；当前 Web 原生文本入口使用它 |
| sdk | SDK 或程序化宿主输入；当前 sdk/cli/process_cli/acp 通道使用它 |
| task-notification | 后台任务通知；当前 heartbeat 分类 |
| scheduled-trigger | 定时任务触发；当前 cron 分类 |
| peer / peer-send-message | 另一会话的输入 / SendMessage 投递；当前内部消息不会进入文本命令入口 |
| projects-relay / coordinator | 协调会话的转发或交接 |
| channel | 外部渠道，必须带 server，例如 feishu、slack |
| observer / observer-activity | 观察代理报告 / 发给观察代理的活动摘要 |
| auto-continuation | 用户操作带来的程序化继续 |
| unclassified | 无法确认来源；当前 TUI 只有通道名而无手势证明时使用它 |
| slack-ping | 会话所有者通过 Slack 发来的 ping |
| plugin | 插件发起，必须带 name；本次没有新增触发这种来源的 Ops |

当前会话身份在 `ops.session_context.session_id`，由平台事件调用绑定；目录来自适配器已绑定的 project_dir，缺失就不填写目录。用户不在 event_input 中传 session_id，也不通过 command/args 选择另一会话。SessionContext 不包含私有原生命令函数、结果对象或 Task。

### 3.3 输出类型、继承和副作用

```python
@dataclass(frozen=True, slots=True)
class ServerRunCommandResult:
    text: str | None = None
    context: tuple[str, ...] | None = None
    ref: int | None = None
```

事件直接返回 ServerRunCommandResult，不包 value/deny，也不能直接返回 dict 或 None。

| 字段 | 类型 / 默认 | 准确含义 | 应返回什么、例子和用途 | 修改规则 |
| --- | --- | --- | --- | --- |
| text | str 或 None；默认 None | 原命令响应中要显示的文字，不是模型的新 query | USER 接管时可返回 "项目检查完成"；"" 表示空文字；None 表示没有文字输出，例如原生命令打开面板。平台沿既有命令响应返回 | USER 可填写自己的文字；替换原生文字时省略 ref |
| context | tuple[str, ...] 或 None；默认 None | 模型可见的备注，每个元素作为一条隐藏 UserMessage 保存，不显示在命令响应里 | 例如 ("后续修改只限 src 目录",)。每个元素须为非空字符串，可包含换行；不填 dict、消息对象或整个会话历史。无需追加备注时省略；追加时使用 (result.context or ()) + ("新备注",) | 省略/None 自动继承下游已有备注；显式提供时须保留完整原前缀，再追加新元素 |
| ref | int 或 None；默认 None | CORE 为本次原生执行产生的引用，让平台保留并使用真实面板、审批块、follow-up 等完整结果；不是可跨调用查询的 ID | USER 原样返回 next 结果时保留 result.ref；直接接管或改写打印文字时省略，保持 None。用户不自行填写 1、2 等编号 | 只能保留本次真实 ref；带 ref 时不能伪造 text |

- `return await next_handler(event)` 使用原生命令结果。CORE 可以打印文字，也可以产生带特殊状态的原生结果。
- `return ServerRunCommandResult(text="已接管")` 且没有调用 next 时，原生命令执行次数为 0。
- next 后要加工文字，返回新的 ServerRunCommandResult，省略 ref；原生 output 为非文字面板数据时保持原数据，不用新文字替换面板；若仍附带原 ref 而修改 text，输出会判为无效。
- next 后省略 context 时，构造结果会继承下游的备注，外层 Handler 也能读取继承后的 context。显式返回 () 不能删除已有备注。
- next 已执行的原生动作不会因为后续换了文字而撤销；平台继续保留审批块、面板/Goal 状态及 follow-up。
- 重复 next 复用第一次真实执行和引用；异常也会缓存，故障隔离不会重复执行原生命令。

Handler 的普通异常或非法输入/输出可能被 Core 隔离并继续默认命令。要主动拒绝，请不调用 next，返回明确的 text，例如 "当前策略禁止该命令"；本事件没有 deny/accepted/code 等输出字段。

### 3.4 可直接安装的 USER Handler

将下面模块与 [manifest.yaml](../../examples/server_run_command/manifest.yaml) 放在 ZIP 根目录安装。在能到达 Worker 原生文本入口的会话输入 `/project_status demo`：返回目录文字，备注进入模型上下文，不执行任何原生命令。

```python
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A Worker slash command served entirely by an installed USER Handler."""

from jiuwenswarm.extensions_v2.event_types.server_run_command import ServerRunCommandResult


class ProjectCommand:
    def register(self, registrar):
        registrar.register_event_handler(
            "server.run_command", self.project_status, {"command": "project_status"},
        )

    async def project_status(self, event, ops, next_handler):
        del next_handler
        cwd = await ops.session.cwd()
        label = event.event_input.args or "current project"
        return ServerRunCommandResult(
            text=f"{label}: {cwd or 'workspace not provided'}",
            context=(f"The project command selected workspace {cwd or 'unknown'} for {label}.",),
        )


def create_extension():
    return ProjectCommand()
```

通过既有 chat.send 的 query 参数传 `/project_status demo`；上游应按原有文本通道把它送到 Worker，不需要新 RPC。完整链为：

Worker 文本输入 → server.run_command → project_status USER → ops.session.cwd → session.cwd 的查询链 → 返回目录 → USER 返回 text/context → 平台逐条保存隐藏 UserMessage → 原命令响应返回。它没有借助用量事件或 ui.render。

### 3.5 改 args 与 next 后加工文字

下面方法可以作为用户扩展的 Handler，按 {"command": "evolve_list"} 注册。它继续已有的演进列表方法；只适合原生返回文字的命令。

```python
from dataclasses import replace
from openjiuwen.core.events import Event
from jiuwenswarm.extensions_v2.event_types.server_run_command import ServerRunCommandResult

async def on_list(event, ops, next_handler):
    value = replace(event.event_input, args=event.event_input.args.strip())
    result = await next_handler(Event(event.event_type, value))
    return ServerRunCommandResult(
        text=(result.text or "") + "\n已完成命令检查。",
        context=(result.context or ()) + ("后续回答应使用当前项目的经验列表。",),
        # Omit ref when replacing the printed output.
    )
```

### 3.6 错误、取消和生命周期

| code / 场景 | 准确含义与处理 |
| --- | --- |
| COMMAND_INVALID_NAME | 宿主传的命令名不合法；使用不带 / 的字母、数字、下划线或短横线名称 |
| COMMAND_UNBOUND_SESSION | 宿主没有有效 session_id；应先由平台绑定真实会话 |
| COMMAND_INVALID_INPUT | 宿主参数、origin 组合或展示事实不合法 |
| COMMAND_UNBOUND_INVOCATION | 缺少本次私有调用帧、Task 不匹配或 CORE 绑定会话不一致 |
| COMMAND_REENTRANT | 当前命令未结束时，对同一会话再次进入宿主命令封装 |
| COMMAND_INVALID_NATIVE_RESULT / COMMAND_INVALID_RESULT | 原生投影或最终输出不符合契约；检查返回类型及 ref/context |
| COMMAND_UNKNOWN | 没有 USER 接管，原生处理也不认识该命令；不作为普通模型 query 提交 |
| COMMAND_CONTEXT_UNAVAILABLE | Handler 返回了备注，但没有可写的当前 Core 上下文引擎；不能宣称备注已生效 |
| 直接 bus.trigger | 没有宿主帧时通常由 Core 输入校验抛出 ValidationError，不是公开业务触发方式 |
| 取消 / 原生异常 | 向原调用方传播，关闭调用帧；不会自动重试或回滚已发生的原生动作 |

最终备注在命令链成功返回后一次写入。备注提交失败会使当前调用失败；命令副作用不能因此撤销。此命令入口继续位于原 Agent 输入之前，不改变 session.start 的既有触发规则；Handler 不应假定命令已经先触发插件初始化。卸载用户包后，其 Handler 不再接管命令；平台不把已保存到模型上下文的用户备注自动删除。

## 4. 方法契约

本次交付的是 Event，没有新增 `ops.server.run_command()`、`ops.command.run()` 或命令注册/列表 Ops。用户只需注册 USER Handler，不调用平台私有封装。

宿主接入其它 Server 命令入口时使用 [dispatch_server_run_command](../../../server/runtime/commands/run_command_events.py)，提供 command、args、origin、presentation、SessionContext，以及真实 execute 和 record_context 函数。execute 接收允许改写的 args，返回 NativeCommandResult；完整原生 value 仅在宿主内部使用。record_context 接收最终备注元组，负责真正落入该平台的模型上下文。

宿主返回的 CommandExecution 是平台适配结果，包含公开 output、私有 native 和 use_native；它不属于用户事件字段，也不是前端接口。当前 DeepAdapter 已提供实际实现和消费位置，其它入口需要自行提供等价的可信适配。

## 5. 接入与兼容方案

### 5.1 启动与各层职责

启动时 bootstrap.register_event_types 登记事件、匹配、输入输出校验及唯一 CORE。既有 ops.build 完成共享 Ops 目录构建；本事件没有新增 BUILTIN，也没有新增 Ops Build 方法。

| 层 / 函数 | 具体责任 |
| --- | --- |
| _handle_slash_command | 原 unary/stream 文本命令实际入口，转入事件适配 |
| run_adapter_slash_command | 保留原路由前提，解析规范命令，收集本次会话/目录/来源，封装原生函数 |
| USER Handler | 策略、参数改写、命令接管、文字加工、模型备注；可调用已有查询 Ops |
| 可选 BUILTIN | 仅在平台另有订阅时存在；本特性没有默认 BUILTIN |
| server_run_command_core | 核对私有调用状态和绑定会话，至多一次执行原生函数，公开投影和本次 ref |
| _handle_native_slash_command | 保留原演进和 TUI Goal 处理逻辑，执行真实命令 |
| dispatch_server_run_command | 校验最终结果，保留原生事实，消费最终备注，返回平台适配结果 |
| append_command_context | 优先复用同一会话的活跃 Session，按既有方式恢复/创建 ContextEngine 上下文，追加 UserMessage 并保存/commit |
| 原 unary/stream 消费者 | 发送原格式响应、审批块或继续已有 follow-up，不把原生命令执行第二次 |

实现位于 event_types、event_handlers 和 server/runtime/commands；EventBus 公共接口、Core 仓库以及前端代码均不需要新增接口。

### 5.2 兼容与迁移

没有用户 Handler 时，已知原生命令通过 CORE 执行原实现。未知斜杠命令在 Extensions V2 已启用且进入本入口时明确报 COMMAND_UNKNOWN；这是对齐 Claude“未知命令拒绝”的行为变化。Extensions V2 不可用时保留原生行为。

文本命令、单独 RPC 和前端/Gateway 命令分属不同入口。本次没有把 command.compact 等 RPC 改成文本命令，也没有改变其压缩流程。调用方不能仅凭事件类型已注册，就推定所有 UI 命令都已接入。

## 6. 验收与未决问题

2026-11-04 合并当前 Server 目标 `a439a6c54`，使用实际 Core `31fee3a86` 重新验证。命令事件/适配 52、compact 15、authorize 35、session.start 18、既有命令 99，共 219 个不同定向用例通过。原有 5 项 compact 私有接口测试已改为真实用户包注册，全部通过，见 [F_28 第 6.5 节](F_28_session-compact.md#65-当前-core-的受管-handler-测试适配)。

扩展全组 990 通过、2 跳过、2 失败；剩余 Store Probe 与 Workbench 的失败在未修改的 Server a439a6c54 / 同一 Core 基线复现，测试 ID 和首行原因一致。五项受管 Handler 测试均包含 USER 完成校验。独立 ZIP、原生命令和隐藏备注恢复也在合并后的代码重新验证通过。

| 场景 | 已验证的实际结果 |
| --- | --- |
| 默认 / 改 args / 可选 BUILTIN / CORE | 原生函数一次执行，绑定会话保持正确，完整原生结果未进入事件字段 |
| USER 接管与 post-next | 不 next 时执行 0 次；加工 text 保留原生副作用，备注按原顺序继承并追加 |
| 字段与 ref 保护 | 伪造命令、来源、布局、ref 或删除备注的 USER 输出被隔离；没有重复原生执行 |
| 重复 next / 多 USER / 子 Task / 重入 / 取消 | 复用真实回执，跨 Task 不借用父调用，取消关闭帧，同会话重入拒绝 |
| 模型备注 | 使用真实 Core ContextEngine/Session 验证 UserMessage(role=user)，已有消息保留，新上下文恢复及首次创建通过 |
| 独立 ZIP | 仓库示例真实安装后调用实际 DeepAdapter._handle_slash_command，触发命令事件与 session.cwd 查询；卸载后不再接管 |
| 原生案例 | 同一独立进程的 /evolve_list 执行真实演进列表代码；Session checkpoint 重建后仍读到隐藏备注 |

独立验收使用 Core 默认 in-memory checkpointer，不把同进程重建恢复扩大成磁盘持久化或进程重启验证。未执行真实模型调用、前端渲染、Gateway 网络往返或未接入的 command.* RPC；ui.render 调用为 0。后续接入其它入口时，需要为该入口补充可信来源、参数适配和结果消费验证。

2026-11-04 文档复核以仓库现有 [F_16 session.append](F_16_session-append.md) 为格式基准，七个主章节保持一致；输入输出表分别写明类型、含义、填写方式/示例和权限。编号对照最新目标 `0c6263bc1`：目标已有 F_01～F_29、S_01～S_16，本 PR 继续使用 F_30 / S_17，文件名、正文、规约、计划和示例引用一致。此轮仅补充文档说明，业务验证仍为本节记录的 a439a6c54 / 31fee3a86，不将文档检查记为重新执行业务测试。

## 7. 参考资料

- [Claude Mods 固定类型声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)：CommandRunInput / Result、CommandPresentation、PromptOrigin、command.run。
- [S_17](../specs/S_17_server-run-command.md)、[输入输出与校验](../../event_types/server_run_command.py)、[CORE](../../event_handlers/server_run_command.py)。
- [可信执行封装](../../../server/runtime/commands/run_command_events.py)、[原生适配和备注写入](../../../server/runtime/commands/adapter_commands.py)。
- [可安装示例](../../examples/server_run_command/README.md)、[原生文本入口](../../../server/runtime/agent_adapter/interface_deep.py)。
- [事件测试](../../../../tests/unit_tests/extensions_v2/test_server_run_command.py)、[适配与模型上下文测试](../../../../tests/unit_tests/extensions_v2/test_server_run_command_adapter.py)。
- [session.append 文档格式](F_16_session-append.md)、[内置事件开发模板](../designs/builtin-event-development-guide.md)。
