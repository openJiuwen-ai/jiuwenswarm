# 会话用量、执行过程和状态条

用户需要上下文用量、Agent 执行过程、工具批次或用量状态条时参考。运行域为 `agentserver`。
片段仅供参考，聚焦业务链路。工厂、完整 manifest 和通用上下文检查沿用 [最小示例](../example.md#1-事件与效果)，正式开发仍需补自己的失败处理和测试。

## 1. 选择事件和 Ops

| 需要 | 事件或 Ops | 分工 |
| --- | --- | --- |
| 页面首次显示、重挂载 | `ui.ready` | 立即查询当前会话，不等下一轮完成才显示 |
| 一轮结束后更新用量 | `context.usage.updated`、`ops.session.usage(breakdown="summary")` | 事件通知变化，Ops 返回当前观测快照 |
| 模型、轮次和消息详情 | `ops.session.model()`、`ops.session.turns()`、`ops.session.messages()` | 按用户确实需要的详情读取 |
| 执行步骤和工具批次 | `turn.step`、`tool.call` | 使用真实事件输入关联步骤、请求和工具批次，不把一次工具调用当成整轮 |
| 状态条与详情页面 | `ops.ui.render`、`ui.click` | 前者发布界面，后者处理已声明的交互 |

只做 Token 状态条时，不必监听步骤和工具事件。需要执行驾驶舱时，再增加对应 Handler 和会话内历史。

## 2. 从页面就绪到用量更新

在 `manifest.ui` 声明状态条；下列对象是 ui 段，不是完整 manifest：

```json
{
  "apiVersion": 1,
  "items": [{
    "type": "web.slot.contribution", "id": "usage",
    "slot": "conversation.input.above", "instanceKey": "usage",
    "renderer": {"kind": "webui-tree"}
  }]
}
```

下面的入口对象保留两条路径：`ui.ready → 查询 → 渲染`，以及 `用量事件 → 刷新已经就绪的实例`。
由工厂返回该对象；不要在工厂里触发业务事件或查询会话。

```python
from dataclasses import replace
from jiuwenswarm.extensions_v2.event_types.context_usage import ContextUsageUpdate
from jiuwenswarm.extensions_v2.webui.models import (
    RenderNode, RenderStatus, UiActionInput, UiActionOutput, UiRenderInput,
)

class UsageExample:
    def __init__(self):
        self.targets = {}

    def register(self, registrar):
        registrar.register_event_handler(
            "ui.ready", self.on_ready, {"target.slot_id": "conversation.input.above"}, "user",
        )
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")

    async def render_usage(self, ops, target):
        usage = await ops.session.usage(breakdown="summary")
        tokens = usage.get("context", {}).get("tokens")
        value = "暂无观测数据" if tokens is None else f"上下文：{tokens} Tokens"
        return await ops.ui.render(UiRenderInput(target, RenderNode("Text", {"value": value})))

    async def on_ready(self, event, ops, next_handler):
        receipt = await next_handler(event)
        request = event.event_input
        if not isinstance(request, UiActionInput) or not receipt.accepted:
            return receipt
        if ops is None or ops.package_metadata is None or ops.session_context is None:
            return UiActionOutput(False, "context_unavailable")
        if request.target.package_id not in ("", ops.package_metadata.package_id):
            return receipt
        target = replace(request.target, package_id=ops.package_metadata.package_id, instance_key="usage")
        if target.session_id != ops.session_context.session_id:
            return UiActionOutput(False, "invalid_session")
        output = await self.render_usage(ops, target)
        if output is None or output.status != RenderStatus.DELIVERED:
            return UiActionOutput(False, "render_failed")
        self.targets[target.session_id] = target
        return receipt

    async def on_usage(self, event, ops, next_handler):
        receipt = await next_handler(event)
        update = event.event_input
        if isinstance(update, ContextUsageUpdate) and ops is not None and ops.session_context is not None:
            target = self.targets.get(update.session_id)
            if target is not None and ops.session_context.session_id == update.session_id:
                await self.render_usage(ops, target)
        return receipt
```

- **状态范围：** 缓存的是普通目标标识，不是 Ops。正式实现还应在会话结束时清理目标和历史，处理并发更新，并按需要保留资源。
- **显示时机：** 页面未就绪时只积累必要数据，不声称已有显示；重新就绪时用当前 Ops 查询并恢复。
- **数据含义：** 未知字段不补零，估算值与真实观测分开说明。

## 3. Token Weather 的设置与详情

需要设置开关时，再增加 `application.settings` 的 item，使用 `scope: "application"`、独立 id，例如 `show-usage`，并填写 `location`、`title` 和 renderer。
设置页的实例与会话状态条是两个目标；设置页不携带聊天会话，不能将它的 target 直接用于状态条。

宿主偏好绑定可让开关与状态条联动。以下片段中的 `target` 指已就绪的状态条目标，`root` 是它的显示内容：

```python
preference = {"key": "visible", "default": True}
setting_root = RenderNode("Switch", {"preference": preference, "ariaLabel": "显示用量"})
usage_request = UiRenderInput(target=target, root=root, visible_when=preference)
```

- 设置 Handler 用设置页自身的 target 渲染 `setting_root`；状态条发布 `usage_request`。相同系统插件身份下的偏好 key 保持一致。
- `visible_when` 与 root 同级，不写进组件 props；`Switch.preference` 不再同时设置 checked/defaultChecked。单纯显示偏好由宿主管理，不必另造后端存储协议。
- 需要详情时，声明 `conversation.panel.right`，使用 Panel 或 Iframe 根。按钮的 `openPanel`、交互 action 和目标实例要对应；只显示状态条时不需要详情页面。
- 使用受管 HTML 时，按“创建目录 → 写入并退出上下文 → 发布 URL → 投递新帧 → 释放旧资源”处理；失败或取消时释放新资源，不提前删除旧页面。

## 4. 执行步骤和工具批次怎样记录

需要执行驾驶舱时，在用量显示之外增加以下观测，不用新的平台事件代替已有能力：

| 事件 | 输入和结果 | 记录方式 |
| --- | --- | --- |
| `turn.step` | `TurnStepInput` 的 turn_id、index、agent_id、model；默认处理的结果可含 answer、usage、tool_uses | 按 turn_id 和步骤序号关联；usage 只使用真实返回的字段 |
| `tool.call` | 输入为一批 `ToolCallEventInput` 组成的 tuple，含 tool_name、tool_call_id、arguments；结果也是批次 | 按 tool_call_id 对应结果，不靠返回顺序，也不把一批工具当作整轮 |

两种输入类型分别从 Core 的 `openjiuwen.core.events.builtins.event_types.turn`、`openjiuwen.core.events.builtins.event_types.tool_call` 导入。

1. 从 `ops.session_context` 确定会话，创建“执行中”记录；只在对应页面已经就绪时渲染。
2. 使用单调计时记录耗时，调用 `await next_handler(event)` 一次。返回后补充结果并标记完成；异常、取消分别记录，然后原样向外抛出。
3. 渲染和摘要整理不改变业务输出，也不因显示失败重试模型或工具。历史条数、参数和结果摘要设上限，敏感字段先脱敏，不默认持久化原始内容。
4. 页面重挂载时重新查询可用快照；没有观察到的历史步骤显示缺失，不用消息数量补造执行记录。

## 5. 验证重点

- 页面刚打开就能显示当前观测，下一轮完成后更新同一实例；切换会话不会串数据，未知值显示未知。
- 增加偏好后，开关只改变约定的显示范围；增加步骤或工具观测后，正常业务仍执行一次，取消和失败不被改写成成功。
