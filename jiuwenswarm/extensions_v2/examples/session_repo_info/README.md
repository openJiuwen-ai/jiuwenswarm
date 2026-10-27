# 会话仓库信息示例 / Session repository information

## 中文

此包订阅已有 `context.usage.updated`，在 USER Handler 中调用
`await ops.session.repo()`，记录当前会话工作目录所在的 Git 工作树根、分支、HEAD
和 detached 状态，然后继续原事件链。用户无需修改 Server、提供上下文、创建 EventBus 或注册 Ops。

在本目录打包，使 ZIP 根目录直接包含 manifest.yaml 与 extension.py：

```powershell
Compress-Archive -LiteralPath .\manifest.yaml, .\extension.py -DestinationPath .\session-repo-info.zip
```

将 ZIP 放到服务运行环境能读取的目录，在现有 Web 命令入口逐条执行：

```text
/extension install "D:\extensions\session-repo-info.zip"
/extension query example.session-repo-info
```

随后正常发送消息。有效原生回合产生用量时，宿主日志出现 `session repo:`。
目录取自宿主绑定的 `ops.session_context.cwd`，Git 信息在 Ops 调用时查询，
不要求 `project_root` 等于 Git 根。独立 worktree 返回自身根；detached 时 branch 为 None；
尚无提交时 head 为 None；无 cwd、非 Git 工作树或裸仓库返回 None。

运行环境须有可执行的 Git。目录失效、Git 缺失、5 秒总超时、所有权拒绝和其它查询失败
以 `SessionRepoError.code` 区分，异常不携带原始 Git 输出。用户可自行捕获并提供降级展示；
示例未捕获时遵循现有 EventBus 的 USER Handler 异常隔离。

查询在线程中执行，不 fetch、checkout、init 或写配置，不返回远端地址，不自动渲染界面。
继承环境中的 `GIT_*` 定向变量被清理；Git 自身的所有权检查仍然有效。
并发 Git 修改期间不承诺跨命令的原子快照。
其它事件只要宿主绑定可信 cwd，其 Handler 同样可调用此 Ops；不必依赖用量事件。

```text
/extension reload example.session-repo-info
/extension uninstall example.session-repo-info
```

## English

This package subscribes to the existing `context.usage.updated` event, calls
`await ops.session.repo()`, logs the result, and continues the original chain.
Place manifest.yaml and extension.py at the ZIP root, install through the existing
`/extension install` command with a server-readable path, then send a normal message.
A native turn with valid usage produces a host log entry.

The host binds the trusted cwd. Git is queried at call time on a thread, with a
five-second total timeout. The result contains repo_root, branch, head and detached.
A linked worktree returns its own root; detached HEAD has no branch; an unborn
branch has no head. No cwd, a non-Git directory or a bare repository returns None.
Unavailable Git/workspaces, timeout, ownership rejection and unexpected failures
raise SessionRepoError with a stable code and no raw Git output.

The query does not initialize, fetch, check out or modify repositories, disclose
remote URLs, or render UI. Inherited GIT_* redirects are removed; Git ownership
checks remain enabled. Concurrent Git writes can change facts between commands.
Handlers on other existing events can reuse this Ops when the host binds a trusted cwd.
