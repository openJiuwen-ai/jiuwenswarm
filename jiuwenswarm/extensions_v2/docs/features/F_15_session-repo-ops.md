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

## 1. 范围、状态与依据

本分支已实现。最近修订 2026-10-31，维护者刘瑜；本次基于已合入 Server `c6a07f909` / Core `bdde48a8`，不依赖未合入的生命周期 PR。公共机制以 [S_16](../specs/S_16_session-query-events.md) 为准，专项字段主责为 [S_05_session-repo-ops.md](../specs/S_05_session-repo-ops.md)；本文字段表提供填写与消费解释，不另立契约。

本次按用户确认的方向，以 Claude Code Mods 2.1.277 的返回形态优先；固定参考提交 `80c6959d28c4c30ecd14fc1cdf172f19d9adad81`。此前直接读实现已改为查询事件链；旧验证不能代替本次事件化验证。统一实施与最终证据见 [F_27](F_27_session-query-events.md)。

## 2. 能力说明与 Claude 对照

`await ops.session.repo()`：根据可信 cwd 查询所在 Git 仓库的身份，适合判断项目来源或展示远端。每次默认真实读取；不执行 fetch、刷新、提交、切分支或修改 Git 配置。

`session.repo` 在每次该 Ops 调用时触发，Handler 接收查询参数，可继续、加工独立返回视图、短路或拒绝；默认 CORE 完成读取，结果由原 Ops 调用者消费。它不是状态变化通知，也不会自动渲染或写入记录。

| 能力 | 默认 CORE 数据来源 | 公开返回 | 与 Claude 的差异 |
| --- | --- | --- | --- |
| session.repo | read_session_repo_view(cwd)：验证工作副本，读取主工作树和 origin；不调用可改写的公共 cwd 查询结果。 | dict 或 None | 四个公开字段及 worktree 主根对齐 Claude；本地没有构建内部仓库清单，internal=False/name=None；URL 会去除潜在凭据。 |

## 3. 事件契约

### 3.1 触发位置与完整流程

```text
示例入口：原生回合完成，平台收集可信会话资料
  → notify_context_usage(...) 安排 context.usage.updated
  → Worker EventBus 给实际 USER on_usage 绑定本次 Ops
  → USER 调用 ops.session.repo()
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

### session.repo：输入

| 字段 | 类型 / 默认 | 谁填写 | 含义与边界 | 怎样填 / 例子 |
| --- | --- | --- | --- | --- |
| 方法参数 | 无 | 调用者不填写 | 查询已绑定会话，不接受 session_id、路径、模型名或包 ID | 直接调用 await ops.session.<方法>() |
| Event.event_input | 空映射 {} | Ops 代理自动构造 | 查询 Handler 收到空参数对象；不是 SessionContext，也不携带可改写目标身份 | 注册时使用 match_pattern={} |

### session.repo：返回值字段

| 字段 | 类型 / 出现条件 | 谁提供 | 准确含义 | 消费方式 / 例子 |
| --- | --- | --- | --- | --- |
| 方法返回 / value | dict 或 None | 由 CORE 或查询 Handler 返回 | 四键仓库身份对象；无 cwd、非 Git 或 bare 仓库为 None；Git 故障是错误 | {'root': 'D:/repo', 'remote': None, 'internal': False, 'name': None} |
| value.root | str，绝对路径 | Git 工作树登记表 | 仓库主工作树根；在 linked worktree 中也返回主工作树，而不是当前 linked 目录 | cwd=D:/linked/src，root 仍可能为 D:/main-repo |
| value.remote | str 或 None | Git origin 配置 | 优先 origin.pushurl，否则 origin.url；没有 origin 为 None。公共 URL 去除凭据及 query/fragment | https://git.example.invalid/owner/repo.git；不是当前分支名 |
| value.internal | bool | 宿主的构建归属策略 | 是否由宿主认可为内部仓库；当前构建无此白名单，默认 False，不按域名猜测 | False；不表示网络连通或权限已通过 |
| value.name | str 或 None | 宿主归属策略命中结果 | 内部白名单命中的 owner/name；internal=False 时为 None，不把任意远端解析成已认可的名字 | 当前默认 None |
| 不再返回的旧键 | repo_root/branch/head/detached | 调用者迁移 | root 替代旧 repo_root 的名称且 worktree 语义变化；不再把分支/HEAD 当作 Claude Repo 字段 | repo['root']；不要再读 repo['branch'] |

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
        registrar.register_event_handler("session.repo", self.on_query, {}, "user")

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
repo = await ops.session.repo()
if repo is not None:
    logger.info("repository: root=%s remote=%s", repo["root"], repo["remote"])
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
| repo | repo_root 改为 root；移除 branch/head/detached 访问。注意 linked worktree 的 root 现在对齐 Claude 的主工作树根；当前执行目录仍由 cwd() 给出。平台内部原 Git 身份读取服务保留。 |

### 5.3 可运行案例与效果

分支示例为 [examples/session_repo_info](../../examples/session_repo_info/)，统一七项示例见 [session_query_audit](../../examples/session_query_audit/)。验证复用 Token Weather 订阅的既有 context.usage.updated 通知作为入口；collect_session_parameters 返回包含七项真实查询结果的参数字典，交给用户自己的 on_parameters 业务函数。测试逐字段核对字典，并将 ui.render 设置为一旦调用就失败；不介入原 Token Weather 渲染代码。

具体例子：平台在一次真实回合后绑定 session-a 的 cwd、项目根、模型/用量观测并触发 context.usage.updated；用户 on_usage 调用 ops.session.repo，代理进入同名查询链，查询 USER 可审计，BUILTIN 计时，CORE 读取原服务；代理解包，用户消费返回参数，再 next 原用量通知。用户只安装自己的包和 Handler，不编写 Adapter 收集资料。

## 6. 验收与未决问题

| 本次实际验证 | 结果 | 范围 |
| --- | --- | --- |
| 扩展模块回归 | 632 项通过 | 生产装配、七项查询、包加载/卸载、原历史与既有扩展行为；源码使用已合入 Core bdde48a8 |
| 相关平台回归 | 140 项通过 | 原生用量入口、历史路径/写入/游标、模型选择迁移及运行配置 |
| 独立进程参数验收 | 通过，7 项返回值逐字段一致 | 真实 ZIP 包→平台 notify→context.usage.updated→七个 Ops/同名查询 Event→返回参数→卸载/释放 |
| 不进入渲染链 | ui.render 调用 0 次 | 验收中 render 一旦被调用即失败；原 Token Weather 渲染文件保持原样 |

专项用例覆盖默认结果、类型/缺失语义、拦截/短路/拒绝、故障隔离、绑定身份、并发/递归/取消、真实包加载与卸载；关联平台用例仍覆盖真实 Git/JSONL 和原存储规则。模型查询验证绑定快照与保存信息，不据此宣称执行了新的模型 API 请求。未执行的浏览器/跨进程用户操作不列为通过。

## 7. 参考资料

### 6.2 当前边界

只提供会话 ID、未提供 cwd 的入口返回 None；本能力不会激活会话来补目录。Git 可访问性、所有权和并发变化仍按真实状态处理；不提供 remote URL、变更文件列表或写操作。

## 7. 参考资料

- [S_05](../specs/S_05_session-repo-ops.md)、[目录契约](../specs/S_03_session-workspace-ops.md)、[Ops](../../handler_ops/session_ops.py)、[Git 服务](../../../server/runtime/session/session_repo.py)。
- [用户示例](../../examples/session_repo_info/README.md)、[通知定义](../../event_types/context_usage.py)、[内置事件接入说明](../designs/builtin-event-development-guide.md)。
- [Claude 固定版本声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)。

- [Claude 会话方法](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L2342-L2447)
- [Claude OpEventOf 与拦截规则](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L5529-L5624)
- [S_16 查询公共协议](../specs/S_16_session-query-events.md)
- [F_27 统一实施记录](F_27_session-query-events.md)
- [内置事件开发指南](../designs/builtin-event-development-guide.md)
