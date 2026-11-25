# 团队、成员和任务关系

用户需要团队状态、成员详情、任务进度或依赖图时参考。运行域为 `agentserver`。
以下片段仅供参考，不附带图形页面、本机服务或完整快照实现。

## 关键写法

在 `register` 中订阅团队更新，在对应页面就绪时读取当前状态：

```python
registrar.register_event_handler("team.monitor_event", self.on_team_event, {}, "user")
registrar.register_event_handler(
    "ui.ready", self.on_ready, {"target.slot_id": "conversation.panel.right"}, "user",
)
```

先确认当前会话与团队的绑定，再在异步 Handler 中读取。下面的 `team_name`、`session_id` 来自这份绑定：

```python
info = await ops.team.info(team_name=team_name)
members = await ops.team.members(team_name=team_name)
tasks = await ops.team.tasks(team_name=team_name, session_id=session_id)
```

- **任务依赖：** 需要画依赖图时，继续核对 `ops.team.task_dependencies` 的参数和返回值，不从任务排列顺序猜前后关系。
- **源码位置：** 事件与 Ops 还涉及 Core 的 `openjiuwen/agent_teams/extensions/` 和 `openjiuwen/extensions/ops_build_handlers/`，不要只搜 Swarm 目录。
- **会话归属：** 优先使用真实绑定或事件记录，不靠自定义团队名猜当前会话；历史默认命名仅在目标实现确认支持时作为回退。
- **显示与失败：** 右侧页面展示真实快照；读取失败时保留上次成功数据并标记过期，没有团队时显示空态。团队事件只更新观测，不重复执行团队业务。
- **资源清理：** 如确需自建只读页面服务，监听端口应由系统分配，并随重载、卸载和关闭释放；普通图表优先复用已有 UI 与受管资源。
