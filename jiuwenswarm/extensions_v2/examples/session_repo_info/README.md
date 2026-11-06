# Git 仓库身份示例

这是用户扩展包，不是产品内置包。订阅既有 context.usage.updated，在自己的 USER Handler 调用公开 Ops；Ops 现在各自派发同名查询 Event，查询 USER→BUILTIN→CORE 返回后由代理解包。用户不创建 Bus、Builder 或 CORE，不改平台 Adapter。

```python
repo = await ops.session.repo()  # root/remote/internal/name，或 None
if repo is not None:
    logger.info("repo root=%s remote=%s", repo["root"], repo["remote"])
```

root 为主工作树根，linked worktree 也返回主根；当前目录另查 cwd。remote 优先 origin.pushurl，并去除 URL 凭据。当前构建无内部仓库清单，internal=False/name=None。旧 branch/head/detached 不再返回。

在本目录打包，让 ZIP 根直接包含 manifest.json 和 extension.py：

```powershell
Compress-Archive -LiteralPath .\manifest.json, .\extension.py -DestinationPath .\session_repo_info.zip
```

```text
/extension install "D:\extensions\session_repo_info.zip"
/extension query example.session-repo-info
```

一次有效原生用量通知后可以在宿主日志感知结果；其它已绑定可信会话的事件同样能调用，不必须先执行 notify。本例保持原通知 next；不调用 ui.render。逐字段含义、默认值、缺失语义和迁移见 [特性文档](../../docs/features/F_15_session-repo.md)，统一七项返回参数示例见 [session_query_audit](../session_query_audit/)。

## English

repo() returns root, remote, internal and name, or None. Linked worktrees report the main root. Origin push URL is preferred and credentials are removed. This build has no internal-repository allowlist; internal is false and name is null. The package only supplies USER handlers. The host collects trusted session context, builds Ops and serves the default CORE. Query events are call interception points, not change notifications. The example logs results and continues the original event without rendering UI. See the linked feature document for field meanings and migration.
