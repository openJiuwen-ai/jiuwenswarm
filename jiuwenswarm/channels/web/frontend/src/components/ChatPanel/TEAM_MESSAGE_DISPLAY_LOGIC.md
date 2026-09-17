# 集群模式消息显示逻辑

本文档总结 ChatPanel 在集群模式下的消息排列意图与当前实现边界。

## 目标

集群模式里，同一轮用户请求会产生多类消息：

- 用户输入
- 成员之间的协作消息
- 工具执行记录
- team_leader 面向用户的阶段性或最终回复
- 普通 assistant 回复

显示层的核心目标是：让用户首先看到“自己问了什么”和“团队最终给了什么”，同时以时间线头像流保留团队内部协作过程。成员协作消息会连续分组，但不会折叠；每条可见消息按真实成员显示头像、名称、时间和内容。

## 消息来源

消息列表由 `MessageList` 统一组织：

- `messages`：聊天消息，来自 store/props。
- `toolExecutions`：工具执行记录，来自 `useChatStore()` 里的 `toolExecutionOrder` 和 `toolExecutions`。

`MessageList` 会先把普通消息和工具执行记录合并成统一时间线，再转成最终渲染项。

## 时间线合并

`buildTimelineItems(messages, executions)` 会做三件事：

1. 过滤掉 `role === 'tool'` 的消息，因为工具消息由工具执行记录单独渲染。
2. 为消息和工具执行记录都生成 `timestampMs`。
3. 按时间升序排序；如果时间缺失或相同，则按原始顺序兜底。

这保证了工具执行与消息在视觉上能按实际发生顺序交错出现。

## 消息分类

集群模式下，系统消息里有两类特殊协议：

- `team.event:`：团队事件消息。
- `team.leader:`：team_leader 面向用户的消息。

`teamEventUtils.ts` 负责解析 `team.event:`：

- `parseTeamEventMessage(message)`：解析事件内容、发送成员、接收成员、时间戳和事件类型。
- `isTeamMemberCollaborationMessage(message)`：判断是否属于成员协作消息。
- `formatTeamEventTime(ts)`：统一格式化事件时间。

其中 `team.event:` 会进一步区分：

- `isLeaderToUser`：`from_member === 'team_leader'`，且不是 p2p/broadcast。这类消息直接作为面向用户的消息展示。
- `isP2P`：事件类型以 `.p2p` 结尾，展示时带 `@目标成员`。
- `isBroadcast`：事件类型以 `.broadcast` 结尾，展示时带 `@所有人`。

## 分段排列规则

`buildRenderItems(items)` 负责把时间线转成三种渲染项：

- `message`：普通消息。
- `toolGroup`：一段连续上下文中的工具执行记录。
- `teamEventGroup`：一段连续上下文中的成员协作消息，渲染时直接逐条展开。

遍历时间线时使用一个 `currentSegment` 暂存当前段落：

- `toolExecutions`：当前段里的工具调用。
- `teamMessages`：当前段里的成员协作消息。
- `messages`：当前段里其他非最终消息。

遇到以下消息时会结束当前段并立即渲染该消息：

- 用户消息：代表一轮新输入开始。
- 最终消息：`assistant` 非流式消息，或 `team-leader-*` 且内容为 `team.leader:` 的消息。

因此一轮对话的典型排列会变成：

```text
用户消息
工具执行组
成员协作消息组
中间系统/过程消息
team_leader 最终消息
```

这个顺序表达了你的核心用意：过程信息被保留在对应轮次里，成员协作消息在同一段里连续展示，不再被折叠成摘要。

## 协作消息组展示

`TeamEventGroupDisplay` 渲染 `teamEventGroup`：

- 不折叠，按原顺序直接显示每条协作事件。
- 每条事件使用 `from_member` 对应的成员头像和名称。
- 连续同一成员的事件可隐藏重复头像，保持时间线紧凑。

单条协作事件中会展示：

- 发送成员头像
- 发送成员
- 时间
- p2p/broadcast 标签
- 消息正文

## team_leader 消息处理

`useWebSocket.ts` 在 team 模式下把 `chat.final` 转成系统消息：

- 内容格式为 `team.leader:{ content, timestamp }`。
- 如果已有正在流式输出的 `team-leader-*` 消息，则更新该消息并结束流式状态。
- 如果没有正在流式的 team_leader 消息，则新增一条系统消息。

`MessageItem` 再根据 `team-leader-*` id 和 `team.leader:` 内容，把它渲染为居中的 team_leader 消息卡片。

## 当前边界

- 只有非 `isLeaderToUser` 的 `team.event:` 会进入协作消息组，并在组内逐条展开。
- `team_leader` 面向用户的消息不会进入协作消息组，保持在主聊天流中。
- 工具执行组和协作消息组都按同一段上下文聚合，直到遇到用户消息或最终消息才 flush。
- 普通 assistant 完成消息会被视为最终消息，防止后续过程消息继续和它混在同一段。
