# 会话 Git 仓库 Ops 规约

| 项 | 值 |
| --- | --- |
| 编号 | S_05 / session-repo-ops |
| 最近修订日期 | 2026-10-27 |
| 主责模块 | `handler_ops/session_ops.py` 与 `server/runtime/session/session_repo.py` |
| 关联特性 | [F_15](../features/F_15_session-repo-ops.md) |

## 1. 接口与结果

`await ops.session.repo() -> dict[str, object] | None`，无会话 ID、路径、Git 参数或超时参数。读取目标为当次宿主上下文的 cwd，目录的可信性沿用 [S_03](S_03_session-workspace-ops.md)。

| 字段 | 含义 |
| --- | --- |
| `repo_root` | 当前 Git 工作树的绝对根路径；linked worktree 返回自身根，不返回共享 Git 管理目录 |
| `branch` | 当前本地分支名；detached 时为 None，不用提交哈希冒充分支 |
| `head` | 查询时 HEAD 的完整对象 ID；无提交仓库为 None |
| `detached` | HEAD 是否处于分离状态；无提交但有符号分支的仓库为 False |

每次查询返回新字典，不持有用户仓库状态，不返回 remote URL 或内部客户端。分支与提交信息可能在查询后立即改变。

## 2. 不变量

- SR-01：能力在既有 ops.build BUILTIN 中加入，不新增同名查询事件，不重复创建 session 命名空间。
- SR-02：只信任绑定 cwd；忽略 event_input 自报路径，不兜底到进程 cwd 或项目根；无项目会话只要有已授权 cwd 也可以查询。
- SR-03：Git 在受信任目录执行固定只读命令；shell=False；清理继承的 Git 控制变量；不绕过 Git 所有权检查。
- SR-04：通过工作树发现支持子目录和 .git 文件形式的 linked worktree；裸仓库不作为会话 Git 工作树返回。
- SR-05：执行在线程中完成，固定总超时为 5 秒。超时结束子进程；调用取消时线程最多继续到该时限。不执行网络、钩子、工作树修改或配置写入。
- SR-06：cwd 是事件快照；仓库状态在查询时读取。并发 Git 写入期间不承诺原子快照；跨会话调用分别按自己的 cwd 查询。

## 3. 缺失和错误

没有 SessionContext：RuntimeError；cwd 为 None：返回 None；cwd 类型或绝对路径非法：TypeError / ValueError。非 Git 工作树或裸仓库：None。

目录不可用、Git 未安装、命令超时及真实 Git 错误抛出 `jiuwenswarm.server.runtime.session.session_repo.SessionRepoError`，其 `code` 分别为 `WORKSPACE_UNAVAILABLE`、`GIT_NOT_FOUND`、`GIT_QUERY_TIMEOUT`、`GIT_QUERY_FAILED`；所有权失败为 `GIT_DUBIOUS_OWNERSHIP`。错误不携带原始 stdout/stderr。USER Handler 的异常传播 / 跳过遵循现有 Core EventBus 故障隔离，不把 Ops 失败自动视作业务拒绝。

## 4. 验证与兼容

示例通过已有用量事件接入，不要求新业务事件。其它已有 Handler 只要绑定可信 cwd，可复用同一方法。与 cwd/root/model 保持共存，原方法语义不变；用户无需设置上下文或修改 notify。执行证据和限制见 F_15。
