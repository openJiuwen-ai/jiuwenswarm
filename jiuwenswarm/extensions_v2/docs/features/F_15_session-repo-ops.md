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

计划验证：中文空格路径的子目录发现、普通分支、完整 HEAD、无提交仓库、detached HEAD、linked worktree、非 Git / 无 cwd、错误 / 超时、Git 环境重定向隔离、并发会话、用户包安装和卸载，以及原生 Adapter 提供 cwd 后的实际调用。

执行环境：Windows、Python 3.11.15、Git 2.55.0.windows.3；从最新扩展分支建立独立工作树，配合 agent-core 的 `rsi_native_agent_os` 源码运行。

| 范围 | 命令 / 入口 | 结果 |
| --- | --- | --- |
| repo 定向测试 | `tests/unit_tests/extensions_v2/test_session_repo_ops.py`，随下行回归命令执行 | 24 项通过 |
| 扩展与原生回归（含上述 24 项） | `python -m pytest -o addopts= -q tests/unit_tests/extensions_v2 tests/unit_tests/agentserver/test_deep_adapter_stream_end_final.py --asyncio-mode=auto -o log_cli=false` | 161 项通过 |
| 用户包独立进程验证 | 将仓库示例的 manifest.yaml / extension.py 打成 ZIP，经真实 `PackageManagementAdapter`、Manager 与 Core Registry 安装；原生 Adapter 消费 Agent 流夹具并发布已有用量事件 | 普通仓库、linked worktree 和无 cwd 三次用户 Handler 结果符合预期，宿主实际日志出现三条记录；卸载后不再执行；HEAD / config / index 内容不变 |

真实 Git 覆盖：中文空格目录、子目录发现、分支 / 完整 HEAD、unborn、detached、`.git` 文件形式的 worktree、非 Git 和裸仓库、查询结果更新、并发会话及 Git 环境重定向隔离。通过真实生产装配安装的 USER Handler 在已有 `ui.ready` 事件查询 repo，并继续其默认 CORE 回执，验证不依赖用量事件；cwd/root/model/ui 能力共存。超时、Git 缺失、所有权与命令失败使用故障注入验证；总时限与线程执行分别验证。

独立进程验证使用真实 Git、包装载和原生通知路径，Agent 输出为可控夹具；未启动真实模型、Gateway / 浏览器网络链路。示例仅写宿主日志，不表示前端界面已接入。外部验证脚本和 JUnit 结果保留于本次交付证据目录，不向源码仓库提交运行日志。

### 6.2 当前边界

只提供会话 ID、未提供 cwd 的入口返回 None；本能力不会激活会话来补目录。Git 可访问性、所有权和并发变化仍按真实状态处理；不提供 remote URL、变更文件列表或写操作。

## 7. 参考资料

- [S_05](../specs/S_05_session-repo-ops.md)、[目录契约](../specs/S_03_session-workspace-ops.md)、[Ops](../../handler_ops/session_ops.py)、[Git 服务](../../../server/runtime/session/session_repo.py)。
- [用户示例](../../examples/session_repo_info/README.md)、[通知定义](../../event_types/context_usage.py)、[内置事件接入说明](../designs/builtin-event-development-guide.md)。
- [Claude 固定版本声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)。
