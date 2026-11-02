# session.end 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_24 / session-end |
| 建档日期 | 2026-10-29 |
| 归档日期 | 2026-10-29 |
| 后续修订 | 2026-10-31，合并至 Server !99；2026-11-02，按 session.append 统一文档格式；同日按用户决定将删除专用通知调整为会话运行结束通知 |
| 计划与进度 | [EXTV2-19 / Server PR10](../designs/extensions-v2-development-plan.md#extv2-19-主会话结束通知与用户资源清理) |
| 主责规约 | [S_13](../specs/S_13_session-end.md) |

## 1. 范围、状态与依据

2026-11-02 修订：保留 session.start 的包激活初始化语义；将 session.end 改为“当前会话运行结束时，给插件一次收尾机会”。Worker 内的 AgentRuntime 释放真实会话运行资源及整体关闭时通知 USER；退出本次运行不要求删除持久历史。同一持久 ID 再次进入 Runtime 后是新代次，可以在新代次结束时再次通知。

首版删除专用实现及其证据保留在第 6 节历史记录，不作为新契约验收。当前实现和本次验证以第 6 节最新记录为准。业务代码在 Server/Runtime；event_types 保留定义、校验和注册，event_handlers 保留默认 CORE。只覆盖主 Agent 会话；不新增前端、同名 Ops 或 Team/子 Agent 结束协议。

## 2. 能力说明与 Claude 对照

Claude Mods 固定提交 bf2323b3021c9d3db7a31e1e7d61068c3f704497（声明头 2.1.277）将 session.end 定义为运行结束的只读通知，输入包含 reason、sessionId、resume，默认返回 sessionId。参考的是小写 Mods 事件；不混用传统大写 SessionEnd Hooks 的字段。

本地平台停稳真实运行后，向 USER 提供结束原因、会话身份与恢复标识。USER 可以保存自己的摘要、释放连接、停止自己的任务，然后 next；CORE 核验可信通知并回显原会话身份，原 Runtime 继续资源释放。存档仍由原平台持有，事件不自动删除或恢复存档。

| 项 | 本次实现 | 与 Claude 定义的关系 |
| --- | --- | --- |
| 触发事实 | 一个已注册 Runtime 会话代次结束；包括真实资源清理和正常 Runtime 停机 | 对齐运行收尾用途，移除“必须永久删除”的前提 |
| 输入 | reason、session_id、resume.id | session_id 为 Python 写法，对应 sessionId；resume.id 是真实恢复身份 |
| 原因 | clear/resume/logout/prompt_input_exit/other | 对齐原因词表；当前原生资源清理、正常 Worker/Runtime 关闭使用 other。没有对应业务时不伪造 clear/resume/logout |
| 输出 | SessionEndOutput(session_id) | 对齐默认回显会话身份；移除 accepted/end_id |
| 运行模式 | Worker 可以同时持有多个会话 | 普通界面切换会保留后台运行，不等同于 Claude 替换当前运行 |
| 时间预算 | 本通知链默认 1.5 秒 | 对齐固定 Claude Mods 的默认结束预算；超时不阻止原释放 |
| CORE 职责 | 验证可信通知、会话及原输入，回显身份；宿主随后继续原释放 | 集成顺序由本地 Runtime 持有，不把 Claude 内部实现当作本地源码 |

session.start 仍按包激活实例初始化一次，与按会话运行代次触发的 end 不一一配对。一个包激活期间可以观察多个会话的多个运行结束。

## 3. 事件契约

### 3.1 触发前后与粒度

```text
真实结束入口：cleanup_session(...) / Runtime.close(...)
  → Runtime Coordinator 停稳目标代次执行，确认 CLOSED
  → 按 session_id + generation 合并同次通知尝试
  → 固定宿主参与者 ExtensionSessionEndParticipant.session_run_ended
  → 平台从目标资料构造 SessionContext、reason、session_id、resume.id
  → bind_end_invocation
  → await Worker bus.trigger(Event("session.end", input), context)
  → USER Handler 保存自己的资料、释放自己的资源
  → await next_handler(event)
  → session_end_core 校验有效调用、原输入及绑定会话
  → 返回实际 SessionEndOutput(session_id)
  → USER 原样返回 → Bus → 宿主通知任务
  → 原 Runtime 继续内存资源清理
  → Runtime 整体关闭时，所有结束通知之后才卸载扩展
```

永久删除在确实终止已注册运行时也经过该通知，随后原删除业务继续；仅有离线存档时不虚构运行结束。普通回复完成、取消当前生成、idle 状态、attach/detach、界面切换不触发。Supervisor 暂时断开而 Worker 常驻不触发；明确 client.disconnect 若实际调用 cleanup_session 终止该运行，则以 other 通知。

同一代次最多尝试一次，重复 cleanup 和随后的 Runtime.close 不重放。恢复同一 ID 会由 Coordinator 分配新 generation，新运行结束再通知。新代次登记等待旧代次的通知和平台清理结束，避免旧清理释放新运行。

### 3.2 输入：每个字段表达什么、由谁填写

实际导入位置是 jiuwenswarm.extensions_v2.event_types.session_end。输入为 frozen dataclass，以下构造器只说明平台生成后的形状，用户无需调用它或自行 trigger：

```python
from jiuwenswarm.extensions_v2.event_types.session_end import SessionEndInput, SessionResume

SessionEndInput(
    reason='other',
    session_id='demo-session-a',
    resume=SessionResume(id='demo-session-a'),
)
```

| 字段 | 类型 / 是否总有 | 准确含义 | 应该填写什么、示例 | Handler 权限 |
| --- | --- | --- | --- | --- |
| reason | str，必填，无省略默认 | 为什么终止当前运行，用于日志、matcher 及选择用户收尾策略；不表示永久删除 | 平台当前资源释放和停机填写 other；有真实清空、替换、登出或主动退出语义的宿主才可分别提供 clear/resume/logout/prompt_input_exit | 只读；不把普通切页改成 resume，不回填 session_deleted |
| session_id | str，必填，无省略默认 | 本次结束的主会话身份，用于定位用户自己按会话保存的缓存、任务和连接；对应 Claude sessionId | Runtime 已登记且已停稳的会话 ID，例如 demo-session-a；不是当前界面 ID、包 ID 或另一目标 | 只读，必须与 ops.session_context.session_id 一致 |
| resume | SessionResume，必填，无省略默认 | 恢复资料对象，说明以后应使用哪个持久会话身份；不携带恢复内容或授权 | 平台填写 SessionResume(id='demo-session-a')；用户访问 event.event_input.resume.id，不当字典索引 | 对象及嵌套 id 均只读 |
| resume.id | str，必填，无省略默认 | 原会话的恢复 ID。可保存到用户自己的摘要中，之后通过平台已有恢复入口使用；不是一次性令牌 | 等于本次 session_id，例如 demo-session-a；无历史、持久化关闭或后续存档被删除时，此 ID 不保证可恢复 | 只读；不能改成另一个会话来读取它的资料 |

reason 词表含义：clear 表示宿主清空并替换旧运行；resume 表示宿主恢复另一个运行并结束旧运行；logout 表示登出结束；prompt_input_exit 表示用户主动退出交互运行；other 表示其他真实结束，例如正常非交互任务完成、Runtime 停机或实际运行资源释放。这是结束原因词表，不代表本产品已经提供全部对应的远程方法或 UI。

#### 本次 Handler 的会话上下文与包身份

平台参与者另外构造可信 SessionContext，并与 Event 一起传给 bus.trigger。Core 为实际进入的 Handler 创建 Ops 并绑定上下文；ops.session_context 是已有只读属性，不收集上下文、不组装事件。

| 字段 | 类型 / 是否必有 | 含义与应该填写什么 |
| --- | --- | --- |
| ops.session_context.session_id | str，必有 | 与输入 session_id 相同，平台确定本次 Ops 查询哪个结束会话；用户只读 |
| ops.session_context.cwd | str 或 None | 此结束入口没有确认活跃执行目录时为 None，不使用 Worker 的进程目录兜底 |
| metadata.project_root | str 或 None | 只使用平台已确认的绝对 project_dir；例如 D:/projects/demo，缺失或相对路径为 None |
| ops.package_metadata.package_id | str，必有 | Core 绑定的实际 USER 扩展包身份，不从用户可改写的事件输入读取 |

### 3.3 输出：每个字段表达什么、如何消费

```python
from jiuwenswarm.extensions_v2.event_types.session_end import SessionEndOutput

SessionEndOutput(session_id='demo-session-a')
```

| 字段 | 类型 / 是否总有 | 含义、填写内容与消费方式 |
| --- | --- | --- |
| session_id | str，必填，无省略默认 | 默认 CORE 原样回显实际结束会话 ID，例如 demo-session-a。USER 必须返回 await next_handler(event) 得到的真实回执；宿主核对该回执，随后继续原资源释放。它不证明所有外部资源已释放，也不表示存档删除成功 |

输入和输出都没有 deny 或 accepted 包装。用户不提前构造假回执、不改写输入、不在 next 之后更换身份。作用域外直接 trigger 不构成合法的运行结束。

### 3.4 USER Handler：保存摘要并释放本包缓存

```python
class SessionSummary:
    def __init__(self):
        self.cache = {}
        self.completed = {}

    def register(self, registrar):
        registrar.register_event_handler('context.usage.updated', self.on_usage, {}, 'user')
        registrar.register_event_handler('session.end', self.on_end, {}, 'user')

    async def on_usage(self, event, ops, next_handler):
        sid = ops.session_context.session_id
        self.cache[sid] = {'project_root': await ops.session.root()}
        return await next_handler(event)

    async def on_end(self, event, ops, next_handler):
        value = event.event_input
        saved = self.cache.pop(value.session_id, None)
        receipt = await next_handler(event)
        self.completed[receipt.session_id] = {
            'reason': value.reason,
            'resume_id': value.resume.id,
            'summary': saved,
        }
        return receipt


def create_extension():
    return SessionSummary()
```

用户在会话 A 中聊天，on_usage 按绑定身份建立自己的摘要。之后 A 的运行资源真实释放，或 Worker 正常关闭，on_end 保存恢复 ID、移除本包的 A 活跃缓存，并取得默认 CORE 回执。平台历史不因本通知删除；以后通过原平台恢复 A，用户可以重新建立活跃缓存。若 A 没有预建缓存，pop 返回 None 仍是合法幂等收尾。示例 completed 仅为包内说明，跨进程保存应使用用户自己的持久化能力。

## 4. 方法能力

### 4.1 用户方法

没有新增 ops.session.end()。用户安装包只注册 USER Handler；不修改 Adapter，不创建 EventBus/Builder，不自行提供平台上下文。复用已有 root、messages 等查询时，仍受其各自契约与结束阶段可用资料约束。没有资料时按相应查询契约返回缺失值，不能初始化另一个会话或模型。

### 4.2 平台内部入口、参数与返回

```python
await runtime.cleanup_session(
    channel_id='web', session_id='demo-session-a',
    end_reason=SessionRunEndReason.OTHER,
)
await runtime.close(end_reason=SessionRunEndReason.OTHER)
```

以上是宿主 Python API 调用形状，不是用户新增远程 Ops。导入 SessionRunEndReason 的位置为 jiuwenswarm.runtime.session_lifecycle。

| 参数 / 返回 | 类型 / 默认 | 准确含义和实际填写内容 |
| --- | --- | --- |
| cleanup_session.channel_id | str，必填 | 原平台已有路由渠道，例如 web；结束事件身份仍来自已登记的运行 |
| cleanup_session.session_id | str，必填 | 平台确实要结束并清理的目标运行；无已注册代次时不触发事件 |
| reset_plan_state | bool，默认 True | 原有清理选项；删除事务使用 False 保留原补偿顺序，与结束原因无关 |
| end_reason | SessionRunEndReason，默认 OTHER | 宿主传入五值之一；无效原因在清理前拒绝。当前产品原生调用保持默认 other，不添加虚假的 clear/resume/logout 请求 |
| cleanup_session 返回 | bool | 原 AgentManager 的资源清理结果，不是事件回执；实际失败或取消仍按原 API 传播 |
| close 返回 | None | 原 Runtime 的关闭结果；不会用 session.end 回执代替实际关闭成功 |

平台参与者接收 SessionRunEndEvent(target, generation, reason)，generation 是 Coordinator 的真实运行代次，只用于宿主去重，不暴露到公共事件输入。Worker 不把普通 foreground_changed 当作结束。

## 5. 实现与兼容

### 5.1 注册与运行职责

Worker 启动时注册事件类型、Matcher、Validator、默认 CORE；既有 ops.build 保存共享能力目录。业务发生时按本次可信上下文创建 Handler Ops，不重跑 ops.build。此通知没有新增业务 BUILTIN。

| 位置 | 实际职责 |
| --- | --- |
| [Runtime 生命周期协议](../../../runtime/session_lifecycle.py) | 定义结束原因、真实代次结束事实、固定宿主参与者接口；不包含 USER 业务 |
| [AgentRuntime](../../../runtime/service.py) | 停稳执行、合并同代次通知；新代次等待旧清理；close 并发通知各会话后继续释放 |
| [平台通知模块](../../../server/runtime/session/end_notifications.py) | 准备可信 SessionContext、输入、1.5 秒预算、调用作用域和包在途计数；实际 trigger |
| [事件定义](../../event_types/session_end.py) | frozen dataclass、匹配、输入输出校验及注册，无业务 trigger |
| [默认 CORE](../../event_handlers/session_end.py) | 校验当前有效宿主调用、完整原输入与绑定会话；创建并复用 SessionEndOutput(session_id) |
| [Worker 停机](../../../server/worker/lifecycle.py) | server.stop / Runtime.close 在 shutdown_extensions 之前，保持 USER Handler 可用 |
| [用户示例](../../examples/session_end/README.md) | 在已有事件准备自己的状态，真实运行结束后保存/释放；不介入 ui.render |

### 5.2 失败、超时和重复

默认等待预算 1.5 秒；USER 异常、非法改写由既有 Core 隔离；平台通知失败/超时记日志后继续原释放。超时使调用作用域失效并请求取消；忽略取消的用户代码可能晚退出，在途计数直到任务真正退出才归零。不能保证撤销它已经提交到外部系统的操作。

关闭入口被取消时保留实际取消语义，不伪造成功回执；已尝试代次不自动重放。Coordinator 停稳超时则不发该代次的正常结束通知。进程崩溃、强制杀进程或不进入正常关闭的信号无法保证 Hook。多个会话的停机通知并发等待，避免按会话数累加预算。

### 5.3 从旧删除版本迁移

| 旧用法 | 新用法 |
| --- | --- |
| reason == session_deleted | 根据真实结束原因处理，当前原生资源释放/停机为 other；需要区分永久删除时继续依赖原删除业务结果 |
| event.event_input.end_id | 移除；不要把 session_id 当作跨所有运行永不重复的通知 ID，用户收尾应幂等 |
| event.event_input.channel_id / mode | 从相应可信平台资料读取需要的信息；本通知只保留 Claude 对应字段 |
| result.accepted | 移除，返回 await next_handler(event) 的真实 session_id 回执 |
| 只有删除会话才清理缓存 | 真正运行结束也清理活跃资源，存档可保留，恢复后重新建立资源 |

## 6. 验收与未决问题

### 2026-11-02 运行结束版本

定向验证：29 项通过，无跳过。覆盖安装的真实 USER 模块、实际 EventBus/CORE/Ops、五种宿主原因、旧字段迁移、身份/恢复资料保护、同代次去重、同 ID 新代次再次结束、回合完成不触发、执行停稳顺序、保留记录文件、新代次等待旧清理、Worker 停机先通知后卸载及多会话并发通知。Runtime 资源管理器在单元场景中是测试替身，不把该结果描述成真实模型或完整产品存档恢复验收。

2026-11-02 最终回归：1362 项通过、2 项平台条件跳过；上述 29 项定向覆盖包含在回归内，不重复累计。范围包含全部 extensions_v2、全部 Runtime、Worker 关闭、原生会话删除/归档和会话切换回归。另有两个未修改的 LoopBeat 50ms 计时用例，在本机原始提交 ce48416582c998ab7301a952ffeb7f86f7f5ef01 的独立工作树同样读到约 47ms；已保留原始失败和基线复现，最终回归显式排除这两项，不修改它们的阈值或业务实现。平台跳过原因见 final.xml。

真实联调：最终源码在两个独立 Front → Supervisor → Worker 生命周期中完成 3 轮 Qwen3.8-Flash 模型对话。第一轮运行持有 A/B，普通回合、界面切换和空闲连接断开不提前发 end；正常停机时两个 USER Hook 都释放了自己实际创建的资源，CORE 回显身份，两个 JSONL 历史文件逐字节保持不变。第二个 Worker 通过原生 session.switch 恢复同一 A ID，继续真实模型对话，旧历史前缀保留；正常停机再次收到 A 的运行结束通知。两次 Worker 内 start 都仅执行一次。所有 Hook 在独立子 Worker PID 中运行；没有 mock Ops、模型或 Adapter，没有手工构造 SessionContext，也没有介入 ui.render。

最终真实证据：D:/work_space/.codex-temp/session-end-run-lifecycle-20261102/real-103525。result.json、initial-result.json、resumed-result.json、原生请求/回复、USER JSONL、源码摘要和两个 cleanup.json 保留证据；两个 Worker 均已退出、端口关闭。real-final-exit.json 确认验证进程退出码为 0。

文档验证：4 段 Python 说明语法、2 个真实输入/输出构造器、文档 USER 案例与实际默认 CORE、60 个本地文档链接通过（其中 4 个跨仓链接按配套 Core 工作树核对）。本地完整调用链 HTML 已同步案例、输入输出字段表和 8 个实际函数；内部锚点/复制按钮有效，start 函数区未变。没有运行格式化或静态检查工具。

当前证据统一保存在 D:/work_space/.codex-temp/session-end-run-lifecycle-20261102：final.log/xml/json、baseline-liveness.log/xml/json、docs-verified.json、verification.json。Core 使用 !110 的 0b5dd43f179be0a41ef2bd4bf0d43950bec9a92f；本项无需新增 Core API，组合 PR !99 仍因 start 保持 Core !110 在先的前置。未执行浏览器 UI、非 Windows 实机或远端 CI；本次不将这些范围写成通过。

### 历史记录：2026-10-29 删除专用版本及之后的文档整理

以下原记录保留追溯。其触发范围、2 秒预算和旧字段均属于旧契约，已经由本次运行结束版本替代，不能用作本次新增入口或格式的通过证据。

<details><summary>展开历史验收记录</summary>

本次 2026-10-31 仅按 F_16 的写法补齐输入/输出字段、注册示例、真实触发条件、Handler 职责和完整业务案例；运行代码与契约不变。文档核对包括 dataclass 字段与示例值、源码节选、Python/YAML/JSON 示例及相对链接。下列回归和真实服务结果均保留原运行日期与范围，不宣称本次重新执行业务或模型验收。

2026-10-31 合并交付验证：本特性与会话查询文档、session.start、session.end、session.attach/session.detach 一起整理到 [Server !99](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/99)，保留四个提交和各自特性文档。合并 PR 中的 session.start 依赖 [agent-core !110](https://gitcode.com/huenrui_/agent-core/merge_requests/110) 的公开 handler_ids；必须先合入 Core !110，再合入 Server。Core 文档因与目标分支重号改为 EV-07 / F_07，接口语义不变。

本次基于 Server `c6a07f909` 和合并最新目标分支的配套 Core，Windows / Python 3.11.15 下 Server 扩展、Runtime、历史、模型上下文与 Gateway 回归 **1744 项通过、3 项跳过**；Core Events/Extension/callback **587 项通过、1 项跳过**。Gateway 的 `test_web_disconnect_unregisters_physical_subscriptions` 在未修改目标分支和合并分支上均因 SERVICE_UNAVAILABLE 失败，最终合跑显式排除该 1 项，未将其计为通过。完整命令与 XML 在 `D:\work_space\.codex-temp\consolidate-server-prs-20261031`：server-final-command.json、server-final-regression.xml、core-command.json、core-regression.xml；基线对照见 gateway-baseline.xml 与 gateway-candidate.xml。本次未重跑真实模型或浏览器，以下旧日期的真实验证保持历史归属。

原独立交付为 Server !93；合并后同一个 RuntimeParticipantRegistry 同时保留固定删除观察者和固定 foreground 观察者。KVC 启停、挂起与 lease 释放的用例同时核对两者不会互相覆盖；本事件自身不要求 handler_ids，也不因此新增 end/detach 联动。

2026-10-29，Windows / Python 3.11.15，使用上文固定的 Server 基线与 Core ead700d7d18a247968d64de54918cf5d201a207a，当前工作区变更包含本特性。404 项扩展/Runtime/删除与归档回归通过，无跳过；另单独重验本特性的 19 项用例通过（包含在该回归范围中，不重复累计）。仅出现外部 Authlib 弃用提示，未运行格式化或静态检查工具。

```powershell
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2 tests/unit_tests/runtime/test_runtime_session_provisioner.py tests/unit_tests/runtime/test_runtime_service.py tests/unit_tests/runtime/test_session_lifecycle_participants.py tests/unit_tests/runtime/test_runtime_architecture.py tests/unit_tests/server/test_archive_lifecycle.py tests/unit_tests/server/test_agent_lifecycle_request.py --asyncio-mode=auto -o log_cli=false
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2/test_session_end.py --asyncio-mode=auto -o log_cli=false
```

| 场景 | 操作与预期 | 实际证据 |
| --- | --- | --- |
| 真用户包与默认 CORE | 安装用户模块，目标/包身份/Ops 正确，next 得到真实回执 | test_session_end.py；真实服务的两个安装包均执行 |
| 重复/并发与失败重试 | 同一未完成删除至多尝试一次，提交后释放状态 | 定向用例与真实 concurrent-delete/repeated-delete 记录 |
| 防改写/短路 | 五个输入字段、提前伪造结果、后置改写及 bool/int 替换无效，CORE 仍处理原目标 | 注册包参数化用例，检查真实调用作用域中的 CORE 回执 |
| 超时/取消 | 合作取消、忽略取消的任务、原请求取消均有明确边界 | 2 秒生产预算的短预算定向构造；超时后不允许迟到 CORE，包计数最终归零 |
| 原生删除顺序 | USER/通知 CORE 完成时目录仍在，随后原 Runtime 实际删除 | 实际 AgentRuntime.delete_session 测试；真实 Worker 读回同样事实 |
| 不误判结束 | 普通 cleanup、断连/重连、归档/恢复不通知 | Runtime 定向测试；真实连接与归档操作，通知计数保持不变 |
| 参与者共存 | KVC 启停、suspend 和 lease release 保留宿主参与者 | 实际 KVCacheApplicationOwner 与 Registry 回归 |
| 作用域与卸载 | 两会话互不串用，Team 排除；卸载后原删除继续 | 定向 Team/跨会话用例；真实卸载后创建并删除第三个会话 |

真实服务证据：`D:\work_space\.codex-temp\server-session-end-event-20261029\real-191323`。实际 Front → Supervisor → 子 Worker → Qwen3.8-Flash 完成两个模型回合；用户包在已有用量事件创建自己拥有的资源文件，在 session.end 内删除它们并取得默认 CORE 回执。断连、归档/恢复不产生结束通知；两个目标均只通知一次，平台目录随后实际删除。现成示例日志出现两次 released=True。未 mock Ops、模型或 Adapter，未手工构造 SessionContext。

result.json、user-handler-results.jsonl、各删除/归档响应、日志和 source-before/source-after 收据记录来源及一致性；cleanup.json 确认 Worker 已退出、端口关闭。首次联调脚本把归档请求误写为单个 session_id，收到 BAD_REQUEST 后已按既有 session_ids 协议修正；该次不计为验收通过，当前结论以上述成功运行记录为准。

已知边界：没有跨进程崩溃/重启 exactly-once；用户资源在取消或平台随后失败时可能已清理，平台不恢复外部副作用。没有真实服务下的恶意无限任务终止保证、非 Windows 实机、产品浏览器 UI、远端 CI 或大并发性能证据。不同 Harness 的统一结束语义、进程退出和恢复资料仍不在首版范围。

2026-11-02 文档整理：以原 session.append 为参照，统一对象示例、五列通知输入表、三列输出表、会话资料说明及具体用户 Handler 案例；保留所有字段来源、默认／缺失、权限、超时和历史验证记录。只改文档，当前契约及 Core !110 前置不变。本轮核对结构、字段、示例与真实默认 CORE 回执、链接及差异，不重跑真实业务、模型或浏览器验收。

</details>

## 7. 参考资料

- [S_13](../specs/S_13_session-end.md)、[内置事件开发指南](../designs/builtin-event-development-guide.md)。
- [Claude 固定声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)、[Claude Mods Session 参考](https://code.claude.com/docs/en/plugins/mods/reference#session)。
- [现成用户包](../../examples/session_end/README.md)、[事件定义](../../event_types/session_end.py)、[默认 CORE](../../event_handlers/session_end.py)、[平台通知](../../../server/runtime/session/end_notifications.py)。
