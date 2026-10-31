# ops.session.repo 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_15 / session-repo-ops |
| 建档日期 | 2026-10-27 |
| 后续修订 | 2026-10-29，完整字段、Git 状态和填写说明；仅文档 |
| 关联功能 PR | PR03 [!81](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/81)，已合入 |
| 主责契约 | [S_05](../specs/S_05_session-repo-ops.md) |

## 1. 范围、状态与依据

本能力已实现并合入。本轮基于最新扩展目标 7795851916dd5ce1d34b70f5a20d6d30eafc25e3 校核，只统一说明，不改变查询代码。查询的是绑定 cwd 所在的最近 Git 工作树，包括 linked worktree，不是模型或项目配置中的任意仓库。

## 2. 能力说明与 Claude 对照

`await ops.session.repo()` 根据可信 cwd 在线程中执行固定只读 Git 命令，返回工作树根、分支、HEAD 和 detached 四字段字典；没有 cwd、非 Git 或裸仓库为 None。它不 fetch、init、checkout、提交、写配置或自动渲染。

Claude 固定版本也提供 repo 查询；本地返回范围只有下面四字段，不返回远端地址或 internal 等额外分类，不宣称可原样套用其他产品的 repo 结构。

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

### 4.1 输入：不接受任何参数

```python
info = await ops.session.repo()
```

用户不填写 repo 路径、cwd、project_root、分支、session_id、Git 参数或超时。平台绑定 SessionContext.cwd，Ops 校验其为本机绝对路径，后台查询再确认目录可访问。不会从 event_input 自报路径、进程当前目录或项目根兜底。

### 4.2 非 None 返回的全部字段

以下全部由平台返回，用户只读取，不填写或回传。每次是新字典，没有持久化缓存；修改返回值不会修改 Git。

| 字段 | 类型 / 何时有值 | 准确含义、合法状态与来源 | 示例与消费方式 |
| --- | --- | --- | --- |
| repo_root | str，非 None 结果必有 | 当前 Git 工作树根的绝对、已解析路径；从绑定 cwd 向 Git 发现，linked worktree 返回自己的根，不返回共享 .git 管理目录 | D:/projects/repo；可能与 root() 的项目根不同 |
| branch | str 或 None，键必有 | 本地符号分支名，已去掉 refs/heads/ 前缀；detached 时为 None；无提交但有符号分支时仍有名字 | feature/demo；None 时不要用 HEAD 哈希冒充分支名 |
| head | str 或 None，键必有 | 查询时 HEAD 的完整对象 ID，不是短 SHA；通常为 40/64 位，不能按固定 7 位截断解释；无提交仓库为 None | 0123456789abcdef0123456789abcdef01234567（示意）；用于关联，不作为写入锁 |
| detached | bool，键必有 | HEAD 是否没有符号分支：branch=None 时 True；尚无提交但存在分支为 False | False；不代表工作树是否 dirty 或本地是否有未提交改动 |

### 4.3 用状态组合解释，不能只看一个 None

| 场景 | repo_root | branch | head | detached |
| --- | --- | --- | --- | --- |
| 普通有提交分支 | 当前工作树根 | 当前分支名 | 完整对象 ID | False |
| 分离 HEAD | 当前工作树根 | None | 完整对象 ID | True |
| 尚无提交的 unborn 分支 | 当前工作树根 | 分支名 | None | False |
| linked worktree | 该 linked worktree 自身根 | 按实际状态 | 按实际状态 | 按实际状态 |

无 cwd、非 Git 目录、裸仓库时整个返回 None，不返回四个空字段。cwd 有值但已不存在/不可访问不是同一种“无仓库”情况，会明确报错。

例如 cwd=D:/projects/repo/app/src，root()=D:/projects/repo/app，repo_root=D:/projects/repo，三者各有含义。cwd 是事件边界快照，Git 分支和 HEAD 是执行查询时读取；并发 Git 修改期间不承诺多个命令组成原子快照。

### 4.4 错误与超时

| 情况 | 结果或异常 | 应如何处理 |
| --- | --- | --- |
| 没有 SessionContext | RuntimeError | 选平台已有会话绑定事件 |
| cwd 为 None | 返回 None | 显示未知，不自行选别的目录 |
| cwd 非字符串/None；空/相对路径 | TypeError / ValueError | 资料格式错误，不视为无 Git |
| 有 cwd 但目录不可用 | SessionRepoError，code=WORKSPACE_UNAVAILABLE | 提示不可访问或用户自行降级 |
| 无可执行 Git | code=GIT_NOT_FOUND | 宿主环境缺少 Git，不返回虚假空仓库 |
| Git 命令共享时限耗尽 | code=GIT_QUERY_TIMEOUT | 默认命令阶段共享 5 秒，不是用户可传参数；此前目录解析/程序发现不在该 deadline 内 |
| Git 所有权拒绝 | code=GIT_DUBIOUS_OWNERSHIP | 不自动改 safe.directory 绕过检查 |
| 其他命令或结果异常 | code=GIT_QUERY_FAILED | 保留明确错误，不展示原始 stdout/stderr |

查询等待取消可传播 CancelledError，正在执行的线程/子进程读取不会因 await 被取消而回滚，Git 命令仍受既有 deadline 约束。USER 未捕获异常沿原 EventBus 故障隔离，不把外层回合继续当作查询成功。

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


### 5.1 用户包与可观察效果

现成包见 [session_repo_info README](../../examples/session_repo_info/README.md)。仓库 USER 示例：

```python
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Log repository identity when a native turn reports usage."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_repo_info")


class SessionRepoInfo:
    """User-owned behavior consuming the host's read-only Git capability."""

    def register(self, registrar):
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")

    async def on_usage(self, event, ops, next_handler):
        info = await ops.session.repo()
        logger.info("session repo: session=%s info=%s", ops.session_context.session_id, info)
        return await next_handler(event)


def create_extension():
    return SessionRepoInfo()
```

示例只写 session repo 日志；需要前端呈现由用户另行接自己的 UI。查询使用 shell=False、清理继承的 Git 定向环境变量、关闭可选锁/fsmonitor，仍保留 Git 所有权检查。结果不是授权凭据、写操作锁或长期稳定保证。

## 6. 验收与未决问题

2026-10-29 本轮只修改文档，校核四字段、状态组合、错误码、5 秒 deadline 范围、无参签名和示例。未重跑历史业务/真实 Git/模型验收；检查证据：`D:\work_space\.codex-temp\server-session-docs-20261029\documentation-verification.json`。

<a id="5-验证设计与执行记录"></a>

### 6.1 原功能验证（2026-10-27，保留原结果）

# session.repo / ops.session.repo 特性设计

# ops.session.repo() / session.repo 特性设计

## 1. 范围、状态与依据

最近修订 2026-10-31。本文仅维护 `ops.session.repo()` 与同名查询事件 `session.repo` 的使用说明、接入和验证边界；一个方法与一个查询事件组成一项特性。开发状态、负责人和交付统一见[开发计划 EXTV2-22](../designs/extensions-v2-development-plan.md#extv2-22-七项只读查询事件化)，交付 [PR !100](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/100)。

公共协议主责为 [S_16](../specs/S_16_session-query-events.md)，专项字段主责为 [S_05_session-repo-ops.md](../specs/S_05_session-repo-ops.md)；下文表格解释怎样填写、怎样消费，不另立契约。共享实现和既有验收记录见[查询公共机制与七项特性索引](../designs/session-query-events.md)。

2026-10-31 按单个 Ops / Event 拆分特性文档；原合篇只作为文档组织历史，不代表仍需一起调用。此次拆分没有改变运行时契约或实现。

## 2. 能力说明与 Claude 对照

`await ops.session.repo()`：根据可信 cwd 查询所在 Git 仓库的身份，适合判断项目来源或展示远端。每次默认真实读取；不执行 fetch、刷新、提交、切分支或修改 Git 配置。

`session.repo` 是这一次查询调用的拦截事件。方法代理验证参数和已绑定会话后触发它；查询 Handler 接收下述参数，可继续默认处理、增加前后操作、返回合法替代视图或明确拒绝。默认 CORE 读取平台事实，结果返回原方法调用者；它不表示状态发生变化，也不自动写历史或显示到界面。

| 项 | 当前默认行为 |
| --- | --- |
| 默认数据来源 | read_session_repo_view(cwd)：验证工作副本，读取主工作树和 origin；不调用可改写的公共 cwd 查询结果。 |
| 方法公开返回 | dict 或 None |
| Claude 对齐与差异 | 四个公开字段及 worktree 主根对齐 Claude；本地没有构建内部仓库清单，internal=False/name=None；URL 会去除潜在凭据。 |

对照依据是 Claude Code Mods 2.1.277 的固定公开类型声明，提交 `80c6959d28c4c30ecd14fc1cdf172f19d9adad81`；不是运行 Claude 私有宿主的结果。

## 3. 事件契约

### 3.1 触发位置、粒度与完整链路

方法代理通过会话、参数及重入检查后，每次调用触发一次 `session.repo`；检查未通过时不会进入查询链。下图展示 Handler 继续默认读取的路径；合法短路或明确拒绝可以在 USER 返回，不进入 CORE。返回箭头对应同一次 await 的结果，不是额外业务通知。

```mermaid
sequenceDiagram
    participant U as 外层用户 Handler
    participant O as Ops 查询代理
    participant B as Worker EventBus
    participant H as 查询 USER / BUILTIN
    participant C as 查询 CORE
    U->>O: await ops.session.repo()
    O->>O: 取已绑定 SessionContext 并校验参数
    O->>B: trigger(session.repo, 参数, context)
    B->>H: 匹配并执行查询 Handler
    H->>C: next_handler(event)
    C-->>H: value 结果
    H-->>B: value 或 deny 封装
    B-->>O: 查询事件结果
    O-->>U: 解包后的值或查询异常
```

平台业务方法先收集可信会话资料并随外层事件传入 Bus，Core 为实际 Handler 创建 Ops。现成示例是 notify_context_usage 发布 context.usage.updated；其他已由平台接好的事件也能使用本方法，前提是本次 Handler 已绑定下表所需资料。用户不需要修改 Adapter、自己创建 Bus，或先调用 notify 来取得上下文。

### 3.2 事件输入、会话上下文与字段说明

| 事件输入字段 | 类型 / 默认 | 谁填写 | 准确含义与改写边界 | 填写示例 |
| --- | --- | --- | --- | --- |
| Event.event_input | 空映射 {} | Ops 代理自动构造 | 查询 Handler 收到空参数对象；不是 SessionContext，也不携带可改写目标身份 | 注册时使用 match_pattern={} |

会话上下文通过 `bus.trigger(Event(...), context)` 的第二个参数传递，与 Event.event_input 分开。下面字段由平台和 Core 绑定，不能通过增加查询参数改换会话或读取目标。

| 对象 / 字段 | 类型与是否必需 | 谁提供 / 表达什么 | 填写或读取示例 |
| --- | --- | --- | --- |
| ops.session_context | Core 只读属性 | 平台随外层事件传入，Core 在执行 Handler 前绑定 | 直接读属性；不是 await 方法，不收集、不组装上下文 |
| SessionContext.session_id | 有效非空 str，必需 | 平台业务边界提供的当前会话身份 | session-a；不从 query 输入或 Hook 返回值改换目标 |
| SessionContext.cwd | str 或 None | 平台绑定的执行目录；从这里查询真实 Git 工作副本 | D:/projects/demo/src；不是用户传入或另一个查询 Hook 返回的目录 |
| current_query_origin().package | str 或 None | Server 查询代理从原调用 Ops 的包身份记录 | 是原发起包，不是当前 Hook 所属包；无 Ops 查询域时 helper 返回 None，须先判空；不是 Core next.origin |

`ops.session_context` 是已有只读属性。业务方法负责采集和组装，查询代理只取出它并继续传给查询事件；Bus 会规范化为只读副本，保留同一会话身份和资料，不承诺前后是同一个 Python 对象。

### 3.3 事件输出、允许的改写、失败与取消

| 事件结果字段 | 类型 | 出现规则 | 准确含义 | 如何消费 |
| --- | --- | --- | --- | --- |
| value | dict 或 None | 成功时唯一顶层键 | 本次查询值；允许 None 的查询仍须包装为 {"value": None} | Ops 会取出 value；调用者不需要再访问 result["value"] |
| deny | 去空白后非空 str，原字符串最多 512 字符 | 明确拒绝时唯一顶层键 | 公开拒绝原因；不能和 value 同时出现 | return {"deny": "QUERY_DISABLED"}；Ops 转为 SessionQueryDenied |

下面的返回字段位于事件结果的 `value` 内。方法成功返回时已经去掉外层 value 包装，因此消费示例中的对象就是方法返回值。

| 返回字段 | 类型 / 出现条件 | 谁提供 | 准确含义 | 消费方式与例子 |
| --- | --- | --- | --- | --- |
| 方法返回 / value | dict 或 None | 由 CORE 或查询 Handler 返回 | 四键仓库身份对象；无 cwd、非 Git 或 bare 仓库为 None；Git 故障是错误 | {'root': 'D:/repo', 'remote': None, 'internal': False, 'name': None} |
| value.root | str，绝对路径 | Git 工作树登记表 | 仓库主工作树根；在 linked worktree 中也返回主工作树，而不是当前 linked 目录 | cwd=D:/linked/src，root 仍可能为 D:/main-repo |
| value.remote | str 或 None | Git origin 配置 | 优先 origin.pushurl，否则 origin.url；没有 origin 为 None。公共 URL 去除凭据及 query/fragment | https://git.example.invalid/owner/repo.git；不是当前分支名 |
| value.internal | bool | 宿主的构建归属策略 | 是否由宿主认可为内部仓库；当前构建无此白名单，默认 False，不按域名猜测 | False；不表示网络连通或权限已通过 |
| value.name | str 或 None | 宿主归属策略命中结果 | 内部白名单命中的 owner/name；internal=False 时为 None，不把任意远端解析成已认可的名字 | 当前默认 None |
| 不再返回的旧键 | repo_root/branch/head/detached | 调用者迁移 | root 替代旧 repo_root 的名称且 worktree 语义变化；不再把分支/HEAD 当作 Claude Repo 字段 | repo['root']；不要再读 repo['branch'] |

继续默认查询使用 `await next_handler(event)`。需要短路时，可直接返回 `{"value": {'root': 'D:/projects/demo', 'remote': None, 'internal': False, 'name': None}}`；需要明确拒绝时返回 `{"deny": "QUERY_DISABLED"}`，下游可以不执行。加工结果必须符合本表的值类型及专项 spec；只改变本次返回视图，不改写平台真实状态、上下文或授权范围。

普通 USER / BUILTIN 异常或无效输出按 Core 隔离规则处理，可能继续默认 CORE；抛异常不能代替明确 deny。CORE / 框架读取失败向调用者传播，不用成功空值掩盖；取消继续传播并在 finally 清理查询调用域。匹配在 trigger 初始固定，next 改写合法参数不会重新选择 Handler；多次 next 可能重复读取，不承诺结果缓存。

### 3.4 可安装用户包中的注册与调用示例

将下面代码作为扩展包的入口模块，并按[现有示例 manifest](../../examples/session_repo_info/manifest.yaml) 的形式指定入口。示例同时展示外层业务 Handler 如何调用本 Ops，以及本查询的 USER Handler 如何继续默认链。

```python
import logging

logger = logging.getLogger(__name__)


class Extension:
    def register(self, registrar):
        # Example outer event: the platform already binds SessionContext.
        registrar.register_event_handler(
            "context.usage.updated", self.on_usage, {}, "user"
        )
        # Intercept this query only.
        registrar.register_event_handler(
            "session.repo", self.on_query, {}, "user"
        )

    async def on_query(self, event, ops, next_handler):
        logger.debug("before query: %s", event.event_type)
        # Continue this query chain; do not call the same public Ops here.
        result = await next_handler(event)
        logger.debug("after query: denied=%s", "deny" in result)
        return result

    async def on_usage(self, event, ops, next_handler):
        value = await ops.session.repo()
        # Use value in your own business code; no ui.render is required.
        logger.info("session.repo result type: %s", type(value).__name__)
        # Continue the original event, not the nested query event.
        return await next_handler(event)


def create_extension():
    return Extension()
```

查询 Hook 想取得默认值应调用 next；在自己的 `session.repo` Hook 中再次调用同名 Ops 会被循环保护拒绝。USER → BUILTIN → CORE 在各层继续时执行，结果反向返回；共享 BUILTIN 负责计时与独立副本，默认 CORE 负责实际读取。

## 4. 方法契约

### 4.1 签名、参数与调用场合

下列是调用侧签名；Core 自动注入的内部 ops 参数不由用户填写。

```python
# Public call signature; no session_id or path argument.
async def repo() -> dict[str, object] | None: ...
```

| 方法参数 | 类型 / 默认 | 谁填写 | 准确含义 | 应该怎样填 |
| --- | --- | --- | --- | --- |
| 方法参数 | 无 | 调用者不填写 | 查询已绑定会话，不接受 session_id、路径、模型名或包 ID | 直接调用 await ops.session.repo() |

典型调用如下；它可写在任何满足会话前提的已注册业务 Handler 中。

```python
repo = await ops.session.repo()
if repo is not None:
    logger.info("repository: root=%s remote=%s", repo["root"], repo["remote"])
```

### 4.2 方法与事件转换、返回值及错误

```text
方法参数 → 查询代理规范化 → Event.event_input
当前 ops.session_context → trigger 的独立 context 参数
  → 同名查询事件 session.repo
事件 {value: 结果} → 参数/结果校验、独立复制、解包 → 方法返回值
事件 {deny: 原因} → SessionQueryDenied(code=DENIED, reason=原因)
```

成功返回 `dict[str, object] | None`，字段及缺失语义见第 3.3 节。无 SessionContext 报 RuntimeError；非法会话 ID 由平台会话校验拒绝；方法参数非法报 TypeError。活跃同名或间接循环报 QUERY_REENTRANT，深度超过 16 报 QUERY_DEPTH_EXCEEDED；不允许调用方覆盖 bus / event_type。平台读取故障保持其原有异常类型和稳定 code。Handler 未捕获 Ops 错误时，还受外层事件自身的故障隔离规则约束，不能把外层继续误认为查询成功。

## 5. 接入与兼容方案

### 5.1 启动登记、上下文绑定与 CORE 分工

平台 bootstrap 向 Bus 登记 `session.repo` 的 Matcher、Validator 和默认 CORE，并登记查询 BUILTIN。Registry.init 触发一次 ops.build；其 CORE 创建 Builder，BUILTIN 将 session.repo 对应的封闭查询代理加入方法目录，Bus 保存 Builder。执行每个实际 Handler 前，Core 用共享目录及本次包身份 / SessionContext 创建新的 Ops。

业务时，用户调用本方法，Core 方法访问器自动把当前 Ops 传给登记的代理；代理读取已绑定上下文并显式 trigger `session.repo`。查询链各实际 Handler 也获得各自的 Ops，查询 CORE 走 `read_session_query` → 校验可信 cwd → `read_session_repo_view(cwd)`，不再调用公共同名 Ops。Builder 保存方法定义，不保存会话查询结果；ops.build 与运行时 builder.build(...) 是两种操作。

统一实现职责见[公共设计第 5 节](../designs/session-query-events.md#5-接入与兼容方案)。默认查询不写业务数据、不发模型请求、不自动渲染；没有全局查询结果缓存。用户包卸载只移除自己的 Hook，不移除平台查询类型或 CORE。

### 5.2 迁移与生效边界

repo_root 改为 root；移除 branch/head/detached 访问。注意 linked worktree 的 root 现在对齐 Claude 的主工作树根；当前执行目录仍由 cwd() 给出。平台内部原 Git 身份读取服务保留。

本次文档拆分不再改变上述兼容边界。执行目录、模型、Git、历史和用量事实仍由平台服务维护；对返回视图的改写不成为另一项平台操作的可信输入。

### 5.3 现成案例和可观察效果

对应用户示例见 [examples/session_repo_info](../../examples/session_repo_info/)，七项联合参数返回示例见 [session_query_audit](../../examples/session_query_audit/)。联合案例中的 collect_session_parameters 是普通用户侧辅助函数，本方法可单独调用；用户业务函数收到返回参数即可验证，不依赖 ui.render。

## 6. 验收与未决问题

### 6.1 本特性的独立验收场景

| 场景 ID | 输入与前置 | 操作 | 预期结果 |
| --- | --- | --- | --- |
| F_15-V-01 | 可信 cwd 位于真实 Git 子目录 / linked worktree | 调用 repo() | 返回四字段对象；root 为主工作树根 |
| F_15-V-02 | 配置含凭据的网络远端；或 cwd 不属于 Git | 调用 repo() | 网络远端剥离凭据；非 Git 返回 None，真实 Git 故障仍报错 |
| F_15-V-03 | 安装仅拦截 session.repo 的用户包 | 分别 next、短路 value、显式 deny | 默认值 / 合法替代值 / SessionQueryDenied 分别可观测 |
| F_15-V-04 | 不同会话绑定不同上下文；或在 Hook 中同名递归 | 分别调用本方法 | 会话隔离；活跃同名或间接循环报 QUERY_REENTRANT，不能无限递归 |
| F_15-V-05 | 读取失败、Hook 异常或查询取消 | 分别构造这些分支 | 按第 3.3 / 4.2 节区分隔离、明确拒绝、平台错误与取消，不把失败当成功空值 |

对应事件化用例位于 [test_session_query_events.py](../../../../tests/unit_tests/extensions_v2/test_session_query_events.py)：包含七项默认调用、各项短路 / 拒绝、非法参数、查询顺序、隔离、循环 / 取消及实际用户包装载卸载。每个场景只验证本方法承诺，不把其他六项也作为调用前置。

### 6.2 已有实施证据与本次文档调整

实现验证基于本 PR 实现提交 `f313f513ee9b`、已合入 Server 基线 `c6a07f909` 与 Core `bdde48a8`，Python 3.11。扩展 632 项、相关平台 140 项及独立进程 ZIP / notify / 七项参数返回验证的实际范围、命令和限制统一保留在[公共设计第 6 节](../designs/session-query-events.md#6-验收与未决问题)。这属于查询事件化实现的既有证据；此次仅拆分文档，不声称重新执行了这些业务测试。

四个公开字段及 worktree 主根对齐 Claude；本地没有构建内部仓库清单，internal=False/name=None；URL 会去除潜在凭据。 该限制属于已知支持边界；此轮未增加新的运行时待决项。未执行 Claude 私有宿主、新模型 API、浏览器或 Front/Supervisor 跨进程验收，不能由文档完整推断这些验收已完成。

## 7. 参考资料

### 6.2 当前边界

只提供会话 ID、未提供 cwd 的入口返回 None；本能力不会激活会话来补目录。Git 可访问性、所有权和并发变化仍按真实状态处理；不提供 remote URL、变更文件列表或写操作。

## 7. 参考资料

- [S_05](../specs/S_05_session-repo-ops.md)、[目录契约](../specs/S_03_session-workspace-ops.md)、[Ops](../../handler_ops/session_ops.py)、[Git 服务](../../../server/runtime/session/session_repo.py)。
- [用户示例](../../examples/session_repo_info/README.md)、[通知定义](../../event_types/context_usage.py)、[内置事件接入说明](../designs/builtin-event-development-guide.md)。
- [Claude 固定版本声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)。

- [Claude 会话方法](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L2342-L2447)
- [Claude OpEventOf 与拦截规则](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L5529-L5624)

- [专项字段规约 S_05_session-repo-ops.md](../specs/S_05_session-repo-ops.md)
- [S_16 查询公共协议](../specs/S_16_session-query-events.md)
- [七项独立特性索引与共享实施记录](../designs/session-query-events.md)
- [Claude 会话方法](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L2342-L2447)
- [Claude 同名 OpEventOf 查询事件](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L5529-L5624)
- [内置事件开发指南](../designs/builtin-event-development-guide.md)
