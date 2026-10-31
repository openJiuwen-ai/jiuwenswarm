# session.repo / ops.session.repo 规约

| 项 | 值 |
| --- | --- |
| 编号 | S_05 / session-repo-ops |
| 最近修订日期 | 2026-10-29 |
| 主责模块 | `handler_ops/session_ops.py` 与 `server/runtime/session/session_repo.py` |
| 关联特性 | [F_15](../features/F_15_session-repo-ops.md) |

最近修订 2026-10-31。七项查询公共机制见 [S_16](S_16_session-query-events.md)，使用说明见 [F_15](../features/F_15_session-repo-ops.md)。本规约维护字段和默认语义，特性文档解释填写方式。

## 1. 能力与默认来源

| Ops / Event | 默认来源 | 返回类型 |
| --- | --- | --- |
| session.repo | read_session_repo_view(cwd)：验证工作副本，读取主工作树和 origin；不调用可改写的公共 cwd 查询结果。 | dict 或 None |

## 2. 输入输出字段

### session.repo：输入

| 字段 | 类型 / 默认 | 谁填写 | 含义与边界 | 怎样填 / 例子 |
| --- | --- | --- | --- | --- |
| 方法参数 | 无 | 调用者不填写 | 查询已绑定会话，不接受 session_id、路径、模型名或包 ID | 直接调用 await ops.session.<方法>() |
| Event.event_input | 空映射 {} | Ops 代理自动构造 | 查询 Handler 收到空参数对象；不是 SessionContext，也不携带可改写目标身份 | 注册时使用 match_pattern={} |

- SR-01：能力在既有 ops.build BUILTIN 中加入，不新增同名查询事件，不重复创建 session 命名空间。
- SR-02：只信任绑定 cwd；忽略 event_input 自报路径，不兜底到进程 cwd 或项目根；无项目会话只要有已授权 cwd 也可以查询。
- SR-03：Git 在受信任目录执行固定只读命令；shell=False；清理继承的 Git 控制变量；不绕过 Git 所有权检查。
- SR-04：通过工作树发现支持子目录和 .git 文件形式的 linked worktree；裸仓库不作为会话 Git 工作树返回。
- SR-05：执行在线程中完成，Git 命令阶段共享 5 秒时限（不含此前目录解析/程序发现）。超时结束子进程；调用取消不撤销线程，命令仍受该时限约束。不执行网络、钩子、工作树修改或配置写入。
- SR-06：cwd 是事件快照；仓库状态在查询时读取。并发 Git 写入期间不承诺原子快照；跨会话调用分别按自己的 cwd 查询。

### session.repo：返回值字段

| 字段 | 类型 / 出现条件 | 谁提供 | 准确含义 | 消费方式 / 例子 |
| --- | --- | --- | --- | --- |
| 方法返回 / value | dict 或 None | 由 CORE 或查询 Handler 返回 | 四键仓库身份对象；无 cwd、非 Git 或 bare 仓库为 None；Git 故障是错误 | {'root': 'D:/repo', 'remote': None, 'internal': False, 'name': None} |
| value.root | str，绝对路径 | Git 工作树登记表 | 仓库主工作树根；在 linked worktree 中也返回主工作树，而不是当前 linked 目录 | cwd=D:/linked/src，root 仍可能为 D:/main-repo |
| value.remote | str 或 None | Git origin 配置 | 优先 origin.pushurl，否则 origin.url；没有 origin 为 None。公共 URL 去除凭据及 query/fragment | https://git.example.invalid/owner/repo.git；不是当前分支名 |
| value.internal | bool | 宿主的构建归属策略 | 是否由宿主认可为内部仓库；当前构建无此白名单，默认 False，不按域名猜测 | False；不表示网络连通或权限已通过 |
| value.name | str 或 None | 宿主归属策略命中结果 | 内部白名单命中的 owner/name；internal=False 时为 None，不把任意远端解析成已认可的名字 | 当前默认 None |
| 不再返回的旧键 | repo_root/branch/head/detached | 调用者迁移 | root 替代旧 repo_root 的名称且 worktree 语义变化；不再把分支/HEAD 当作 Claude Repo 字段 | repo['root']；不要再读 repo['branch'] |

## 3. 事件、可信绑定与错误

每个公开 Ops 触发同名查询 Event。USER 返回 value/deny 封装，Ops 解包；默认 CORE 调用原始服务，不经公共 Ops 再次发起同名查询。调用者不填 session_id，不从返回视图决定平台真实身份或授权根。缺失值、真实零值、读取故障不同；拒绝和重入见 S_16。查询结果不写平台数据、不自动渲染。

## 4. 兼容迁移与差异

示例通过已有用量事件接入，不要求新业务事件。其它已有 Handler 只要绑定可信 cwd，可复用同一方法。与 cwd/root/model 保持共存，原方法语义不变；用户无需设置上下文或修改 notify。执行证据和限制见 F_15。

## 字段填写与文档同步（2026-10-29）

用户不填路径、分支、Git 参数或超时。返回 repo_root/branch/head/detached，普通、detached、unborn、linked worktree 的组合见 F_15 第 4 节。head 是完整对象 ID，None/False 不互相代替；repo_root 不必等于项目根。Git 命令阶段共享 5 秒 deadline，不包含此前目录解析/程序发现；取消不回滚已经运行的线程。所有字段为平台只读返回，不触发新事件。

本轮仅文档，不改变业务代码，未重跑历史业务验证。完整说明按七段式编写指南维护，历史验收保留原日期和范围。

| 方法 | 迁移 | Claude 对照差异 |
| --- | --- | --- |
| repo | repo_root 改为 root；移除 branch/head/detached 访问。注意 linked worktree 的 root 现在对齐 Claude 的主工作树根；当前执行目录仍由 cwd() 给出。平台内部原 Git 身份读取服务保留。 | 四个公开字段及 worktree 主根对齐 Claude；本地没有构建内部仓库清单，internal=False/name=None；URL 会去除潜在凭据。 |
