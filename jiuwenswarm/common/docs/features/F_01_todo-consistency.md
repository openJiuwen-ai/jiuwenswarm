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

Todo准备失败时，宿主通过 `prepare_todo_context(..., callback_context=ctx)`设置force_finish(error)并保留异常；普通SDK rail采取等价处理。拒绝仅抛异常的方案，因为回调框架会记录异常后继续执行，导致模型重复调用必败工具。普通、Flash、Code真实回调边界均验证模型调用次数为零。

JiuwenBox配套沿现有鉴权/路径策略进入daemon执行CAS，不在宿主映射路径后替换、不回退普通上传、不因响应丢失重放写入。已验证配套非editable core/Box wheel，实际独立daemon协议测试不安装namespace；Windows原生权限仍待验收。正式lock仍需可获取的新core版本。

实现与证据见工作区 `Feat/req01-todo-edit/04-implementation.md` 和 `checkpoint/feat/req01-todo-edit/validation/`。WSL Chrome已登录，首次真实任务失败并停止；仓内协议缺口已补齐，实际Linux沙箱仍受网络命名空间权限阻塞，现有运行进程尚未加载新实现。宿主扩大组合296项通过、配套wheel宿主50项通过；不代表产品业务验收。执行者未创建提交或PR。
