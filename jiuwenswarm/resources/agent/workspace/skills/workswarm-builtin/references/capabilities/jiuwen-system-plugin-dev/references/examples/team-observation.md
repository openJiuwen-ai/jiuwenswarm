# 团队、成员和任务关系

用户需要团队状态、成员详情、任务进度或依赖图时参考。运行域为 `agentserver`。
以下片段仅供参考，保留“确定团队 → 查询快照 → 渲染页面 → 事件刷新”的过程，不附带图形页面或本机服务。

## 1. 事件与 Ops 怎么分工

| 入口 | 使用方式 |
| --- | --- |
| `ui.ready` | 页面就绪时查询，不要求恰好有新的团队事件 |
| `team.monitor_event` | 从 `TeamMonitorEventInput.team_name` 和当前调用的会话上下文识别更新范围；记录事实、刷新已打开的页面 |
| `ops.team.info(team_name=...)` | 读取团队信息 |
| `ops.team.members(team_name=...)` | 读取成员名册 |
| `ops.team.tasks(team_name=..., session_id=...)` | 读取指定团队、指定会话的任务 |
| `ops.team.task_dependencies(task_id, team_name=..., session_id=...)` | 读取一项任务的直接依赖，不从任务显示顺序猜前后关系 |

团队源码还涉及 Core 的 `openjiuwen/agent_teams/extensions/` 和 `openjiuwen/extensions/ops_build_handlers/`。
页面请求未必绑定“当前团队”，因此查询时显式传 team_name；团队名不能替代 session_id。

## 2. 声明页面并读取真实快照

在 `manifest.ui.items` 中放入右侧页面贡献，其他顶层声明沿用 [最小示例](../example.md#1-事件与效果)：

```json
{
  "type": "web.slot.contribution", "id": "team-summary",
  "slot": "conversation.panel.right", "instanceKey": "team-summary",
  "renderer": {"kind": "webui-tree"}
}
```

下面是入口对象中的方法，`register` 将 `ui.ready` 绑定到 `on_ready`。
`self.session_teams` 只保存已经确认的“会话 ID → 团队名”映射；入口对象创建时将其设为空，不把猜测的团队名当成绑定。

```python
from dataclasses import replace
from jiuwenswarm.extensions_v2.webui.models import (
    RenderNode, RenderStatus, UiActionInput, UiActionOutput, UiRenderInput,
)

async def on_ready(self, event, ops, next_handler):
    receipt = await next_handler(event)
    request = event.event_input
    if not isinstance(request, UiActionInput) or not receipt.accepted:
        return receipt
    if ops is None or ops.package_metadata is None or ops.session_context is None:
        return UiActionOutput(False, "context_unavailable")
    if request.target.package_id not in ("", ops.package_metadata.package_id):
        return receipt
    sid = ops.session_context.session_id
    if request.target.session_id != sid:
        return UiActionOutput(False, "invalid_session")
    team_name = self.session_teams.get(sid)
    if team_name is None:
        value = "当前会话尚未绑定团队"
    else:
        info = await ops.team.info(team_name=team_name)
        if info is None:
            value = "当前团队不可读取"
        else:
            members = await ops.team.members(team_name=team_name)
            tasks = await ops.team.tasks(team_name=team_name, session_id=sid)
            value = f"团队：{team_name}，成员：{len(members)}，任务：{len(tasks)}"
    root = RenderNode("Panel", {"title": "团队观察"}, children=(RenderNode("Text", {"value": value}),))
    target = replace(request.target, package_id=ops.package_metadata.package_id, instance_key="team-summary")
    output = await ops.ui.render(UiRenderInput(target, root))
    if output is None or output.status != RenderStatus.DELIVERED:
        return UiActionOutput(False, "render_failed")
    return receipt
```

这个片段先显示摘要，确认读取与渲染链路正确，再根据 tasks 和依赖边开发图形。不要先写整套图形服务，再发现读取的团队或会话不对。

## 3. 团队事件怎样推动更新

登记 `team.monitor_event` 时使用独立 Handler，继续默认链并保留其返回值，不能返回 UI 回执代替团队事件输出。

1. 从当前 `ops.session_context.session_id` 和 `TeamMonitorEventInput.team_name` 取得真实关联；缺少会话上下文时不猜绑定。
2. 缓存这份关联以及普通快照、目标标识，不缓存本次 Ops。一个会话涉及多个团队时，保留用户已选目标，不能用最新事件直接覆盖。
3. 对已经就绪且对应此团队、会话的页面，使用本次有效 Ops 重新查询并渲染；页面尚未打开时只更新必要数据。
4. 刷新或重载后，优先从宿主已有绑定或自己保存的已确认关联恢复；无法恢复时显示空态，不根据自定义团队名反推会话。

任务详情或依赖图应将目标 team_name、session_id 一起传入相关查询。返回的是公开视图，不直接修改团队数据库，也不在观测 Handler 中重新执行任务。

## 4. 验证重点

- 团队事件先到、页面先打开、没有团队三种顺序都能处理；切换会话或团队不会串数据。
- 读取失败时保留上次成功快照并标记过期；首次读取失败显示明确错误，不伪造零任务或全部完成。
- 成员状态、任务进度、依赖方向以公开数据为准，不能把缺少事件直接解释为业务已经结束。
- 页面优先复用已有组件或受管资源；确需本机服务时，再补端口分配、取消、重载、卸载和关闭清理，不默认复制整套服务。
