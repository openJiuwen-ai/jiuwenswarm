# Git 仓库身份示例

这是用户扩展包，不是产品内置包。订阅既有 context.usage.updated，在自己的 USER Handler 调用公开 Ops；Ops 现在各自派发同名查询 Event，查询 USER→BUILTIN→CORE 返回后由代理解包。用户不创建 Bus、Builder 或 CORE，不改平台 Adapter。

```python
repo = await ops.session.repo()  # root/remote/internal/name，或 None
if repo is not None:
    logger.info("repo root=%s remote=%s", repo["root"], repo["remote"])
```

root 为主工作树根，linked worktree 也返回主根；当前目录另查 cwd。remote 优先 origin.pushurl，并去除 URL 凭据。当前构建无内部仓库清单，internal=False/name=None。旧 branch/head/detached 不再返回。

在本目录打包，让 ZIP 根直接包含 manifest.yaml 和 extension.py：

```powershell
Compress-Archive -LiteralPath .\manifest.yaml, .\extension.py -DestinationPath .\session_repo_info.zip
```

```text
/extension install "D:\extensions\session_repo_info.zip"
/extension query example.session-repo-info
```

随后正常发送消息。有效原生回合产生用量时，宿主日志出现 `session repo:`。
目录取自宿主绑定的 `ops.session_context.cwd`，Git 信息在 Ops 调用时查询，
不要求 `project_root` 等于 Git 根。独立 worktree 返回自身根；detached 时 branch 为 None；
尚无提交时 head 为 None；无 cwd、非 Git 工作树或裸仓库返回 None。

运行环境须有可执行的 Git。目录失效、Git 缺失、Git 命令阶段共享 5 秒时限、所有权拒绝和其它查询失败
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
five-second shared deadline for Git commands. The result contains repo_root, branch, head and detached.
A linked worktree returns its own root; detached HEAD has no branch; an unborn
branch has no head. No cwd, a non-Git directory or a bare repository returns None.
Unavailable Git/workspaces, timeout, ownership rejection and unexpected failures
raise SessionRepoError with a stable code and no raw Git output.

The query does not initialize, fetch, check out or modify repositories, disclose
remote URLs, or render UI. Inherited GIT_* redirects are removed; Git ownership
checks remain enabled. Concurrent Git writes can change facts between commands.
Handlers on other existing events can reuse this Ops when the host binds a trusted cwd.

## 字段填写速查 / Field guide（2026-10-29）

repo() 无参数；返回 repo_root、branch、head、detached 四键。detached 的 branch=None；unborn 的 head=None、detached=False；linked worktree 返回自身根。无 cwd/非 Git/裸仓库整个为 None，可访问性或 Git 错误另报异常。Git 命令阶段共享 5 秒，不作为用户参数。 用户不填写这些返回字段，不传其他会话或路径；修改返回副本不会改变平台事实。

每个字段的类型、默认/缺失、合法值、来源和示例，以及完整注册/调用链见 [特性文档第 4、5 节](../../docs/features/F_15_session-repo-ops.md)。本轮仅文档，历史业务验证未重跑。

English: the query takes no arguments. All returned fields are host-provided facts, not caller input. Read the linked field tables for precise types, nullability, source and state combinations. Returned data does not activate a model, modify Git or render UI. This revision only clarifies documentation; historical runtime evidence retains its original date and scope.

一次有效原生用量通知后可以在宿主日志感知结果；其它已绑定可信会话的事件同样能调用，不必须先执行 notify。本例保持原通知 next；不调用 ui.render。逐字段含义、默认值、缺失语义和迁移见 [特性文档](../../docs/features/F_15_session-repo-ops.md)，统一七项返回参数示例见 [session_query_audit](../session_query_audit/)。

## English

repo() returns root, remote, internal and name, or None. Linked worktrees report the main root. Origin push URL is preferred and credentials are removed. This build has no internal-repository allowlist; internal is false and name is null. The package only supplies USER handlers. The host collects trusted session context, builds Ops and serves the default CORE. Query events are call interception points, not change notifications. The example logs results and continues the original event without rendering UI. See the linked feature document for field meanings and migration.
