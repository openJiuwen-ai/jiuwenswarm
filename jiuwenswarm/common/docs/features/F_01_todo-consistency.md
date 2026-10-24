# Todo 增量更新与并发一致性

| 项 | 值 |
| --- | --- |
| 类型 / 编号 | feature / F_01 |
| 归档日期 | 2026-10-24 |
| 关联需求 | FE20261014380870 |
| 关联模块 | jiuwenswarm/common |

多个工具从旧列表分别整表保存，会丢失已经成功的更新；回放旧事件又会使展示回退。现在 SDK 在同一存储对象上执行读、增量变换和条件原子替换，事件随提交 receipt 携带身份和 revision。宿主负责捕获执行归属、迁移特殊写入方、读取历史及恢复通知。

版本化格式显式激活，默认仍为数组。拒绝仅用模型提示、工具实例锁、UI 状态取最大值和读失败清表；它们无法同时解决持久化竞争、合法 reopen 与恢复。采用同文件元信息避免双文件提交，完整快照避免事件重放累计误差；代价是本地 CAS 读核对与配套版本发布。

`OPENJIUWEN_TODO_V2` 属于 Track A 启动环境，Relay 的 shared/launch/spawn 白名单一致传递，不能在会话业务配置中热切换。启动 bootstrap 的旧读缓存跳过具备原生事务和版本读取能力的工具/rail。

实现和本地证据见工作区 `Feat/req01-todo-edit/04-implementation.md` 与 `checkpoint/feat/req01-todo-edit/validation/`。已验证本地非 editable SDK wheel；正式 lock 仍需新 core 可获取版本。Linux 隔离实例曾启动，登录未成功，随后按用户要求停止并暂缓实测；远程沙箱、Windows 包与真实业务验收未完成。未创建提交或 PR。
