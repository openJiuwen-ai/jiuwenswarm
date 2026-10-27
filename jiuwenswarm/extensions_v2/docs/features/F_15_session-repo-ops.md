# 会话 Git 仓库只读查询设计方案

| 项 | 值 |
| --- | --- |
| 编号 | F_15 / session-repo-ops |
| 建档日期 | 2026-10-27 |
| 关联计划 | [EXTV2-15](../designs/extensions-v2-development-plan.md) |
| 主责规约 | [S_05](../specs/S_05_session-repo-ops.md) |

## 1. 背景、目标与范围

PR-01 提供执行目录与项目根，PR-02 提供模型资料；用户仍无法通过标准 Ops 判断该执行目录属于哪个 Git checkout、当前分支和提交。项目根与 Git 根并非同一概念，独立 worktree 也不应返回主仓库路径。

本特性增加无参数的 `await ops.session.repo()`。目标只来自宿主绑定的 `SessionContext.cwd`，在查询时读取最近的 Git 工作树信息，返回普通字典或 None。不新增查询 Event、业务 CORE 或前端接口，不实现另行讨论的公共会话查询 RPC。

## 2. 方案与取舍

- D-01：向已有 `session_ops_build` 添加 repo 方法，复用 Core 的唯一一次 `ops.build`、包加载、会话绑定与方法代理。原 cwd/root/model 保持不变。
- D-02：新增中立只读模块 `server/runtime/session/session_repo.py`，在 `asyncio.to_thread` 中执行有总时限的固定 Git 命令。该模块不依赖 EventBus 或 Agent。
- D-03：复用现有 ProjectGit 的 Git CLI 探测思路与 `git_safe_directory` 错误识别；不直接复用 `ProjectGitService.probe/status`，因为其业务要求登记项目，查询可能写项目快照，并包含本特性不需要的文件状态及分支枚举。
- D-04：移除继承环境中的 Git 定向 / 注入变量，固定命令与 UTF-8 / C locale，不使用 shell，不修改 safe.directory，不返回原始命令输出。避免宿主的 GIT_DIR 等把查询导向别的仓库。
- D-05：只返回 repo_root、branch、head、detached；无远端 URL、凭据、文件清单或 diff。无 cwd、非 Git 工作树和裸仓库返回 None；工具缺失、超时、目录失效和真实命令失败显式报错。

仓库由受信任 cwd 向上发现，repo_root 可以是 cwd 的祖先；宿主负责授权该工作目录与其仓库身份查询。用户事件载荷中的路径不参与目标选择。不把 metadata.project_root 当作 Git 根，也不把缺失 cwd 兜底到宿主进程目录。

## 3. 数据与调用流程

Worker 初始化 → 既有 ops.build BUILTIN 登记 repo → 安装用户扩展 → 宿主在业务事件传入可信 cwd → Core 为 USER Handler 绑定 Ops → `ops.session.repo()` → 平台只读查询 → 返回结果 → 用户按需继续原事件链。

目录取自事件边界快照，Git 分支与提交取自 Ops 查询时刻。查询不锁定用户仓库，也不承诺并发 checkout/commit 期间的跨命令原子快照；调用方不可将结果作为 Git 写操作的锁或授权凭证。

## 4. 代码与用户接入

- `handler_ops/session_ops.py`：能力函数、可信上下文校验与共享 Builder 登记。
- `server/runtime/session/session_repo.py`：Git 工作树发现、分支 / HEAD 读取、环境隔离、超时和安全错误。
- `examples/session_repo_info/`：可安装的日志示例，复用既有 `context.usage.updated`，用户不修改 Server、不创建 Bus、不注册 Ops。
- 测试：真实临时仓库、linked worktree、detached / unborn、并发、错误与真实用户包调用。

```python
async def on_usage(event, ops, next_handler):
    info = await ops.session.repo()
    # None means no bound cwd or no Git work tree; errors remain explicit.
    return await next_handler(event)
```

## 5. 验证设计与执行记录

计划验证：中文空格路径的子目录发现、普通分支、完整 HEAD、无提交仓库、detached HEAD、linked worktree、非 Git / 无 cwd、错误 / 超时、Git 环境重定向隔离、并发会话、用户包安装和卸载，以及原生 Adapter 提供 cwd 后的实际调用。

执行环境：Windows、Python 3.11.15、Git 2.55.0.windows.3；从最新扩展分支建立独立工作树，配合 agent-core 的 `rsi_native_agent_os` 源码运行。

| 范围 | 命令 / 入口 | 结果 |
| --- | --- | --- |
| repo 定向测试 | `tests/unit_tests/extensions_v2/test_session_repo_ops.py`，随下行回归命令执行 | 24 项通过 |
| 扩展与原生回归（含上述 24 项） | `python -m pytest -o addopts= -q tests/unit_tests/extensions_v2 tests/unit_tests/agentserver/test_deep_adapter_stream_end_final.py --asyncio-mode=auto -o log_cli=false` | 161 项通过 |
| 用户包独立进程验证 | 将仓库示例的 manifest.yaml / extension.py 打成 ZIP，经真实 `PackageManagementAdapter`、Manager 与 Core Registry 安装；原生 Adapter 消费 Agent 流夹具并发布已有用量事件 | 普通仓库、linked worktree 和无 cwd 三次用户 Handler 结果符合预期，宿主实际日志出现三条记录；卸载后不再执行；HEAD / config / index 内容不变 |

真实 Git 覆盖：中文空格目录、子目录发现、分支 / 完整 HEAD、unborn、detached、`.git` 文件形式的 worktree、非 Git 和裸仓库、查询结果更新、并发会话及 Git 环境重定向隔离。通过真实生产装配安装的 USER Handler 在已有 `ui.ready` 事件查询 repo，并继续其默认 CORE 回执，验证不依赖用量事件；cwd/root/model/ui 能力共存。超时、Git 缺失、所有权与命令失败使用故障注入验证；总时限与线程执行分别验证。

独立进程验证使用真实 Git、包装载和原生通知路径，Agent 输出为可控夹具；未启动真实模型、Gateway / 浏览器网络链路。示例仅写宿主日志，不表示前端界面已接入。外部验证脚本和 JUnit 结果保留于本次交付证据目录，不向源码仓库提交运行日志。

## 6. 限制与遗留

其它仅提供会话 ID 的入口仍无 cwd，repo 返回 None；该特性不隐式查询或激活会话。Git 元数据读取依赖本机 Git、目录可访问性与 Git 自身所有权检查。没有任何 fetch、init、checkout、提交、索引刷新写入或 Git 配置写入。
