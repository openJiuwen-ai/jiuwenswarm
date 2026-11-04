# 应用角色会话

纯文字 NPC 可以使用轻量角色会话。它保留精简的基础系统提示、应用配置的角色规则及自身对话历史，避免加载开发助手、项目指令、个人记忆、技能和工具提示。

通过既有 Gateway Web `/ws` 创建：

```json
{
  "method": "session.create",
  "params": {
    "create_token": "<稳定角色创建token>",
    "execution_profile": "app_role",
    "role_instructions": "扮演岛民阿海，语气平和。只返回包含 reply 和 emotion 的 JSON 对象。",
    "project_id": "<创建应用的会话所属项目ID>",
    "work_mode": "<该项目的code或work模式>",
    "persist_session": false,
    "mode": "agent",
    "title": "微缩生活岛·老渔夫"
  }
}
```

## 团队 NPC

创建应用时可按玩法选择团队 NPC。沿用上面的创建参数，将 `mode` 改为 `"team"`，并传入固定名册，例如：

```json
"npc_team": {"members": [{"name": "fisherman", "title": "老渔夫", "role_instructions": "以老渔夫身份发言。", "desc": "熟悉海况"}]}
```

主 NPC 是 Team leader，`role_instructions` 和 `title` 定义它的人格与署名。1–5 个从 NPC 对应同一 Team 的原生成员会话，分别使用名册中的人格。宿主在首次对话时装配团队。创建响应须确认 `execution_profile: "app_role"`、`session_id` 和 Team 模式（`team.work.normal` 或 `team.code.normal`）。后续仅向该会话发送玩家文本或字符串形式的场景 JSON，沿用返回的模式。

每轮由 leader 给各成员安排发言。每条非空 `chat.final` 都是该角色自己的最终文本，事件含 `member_name`、`display_name` 和 `content`。按会话及回合关联（优先 `turn_request_id`），逐条显示署名和文本。等待 `chat.processing_status` 的 `is_processing:false` 结束回合，检查所有预期角色是否已发言。应用前端仅渲染最终发言与必要状态，内部上下文、推理、工具与任务记录留在宿主。删除应用绑定的 Team 会话即进入原有 Team 删除流程。

## 旧版轻量咨询

`mode:"agent"` 携带成员名册时沿用原来的独立角色会话与 `ask_<name>` 咨询协议。新应用需要各角色独立发言时使用上述 Team 模式。成员名册创建后固定；两种模式均拒绝请求注入宿主工具。

应用服务端从 `WORKSWARM_SOURCE_SESSION` 获得来源会话 ID，再通过 `session.get_metadata` 查询项目与工作模式。角色的执行配置与项目归属独立：仍在创建应用的项目下展示。不要固定项目目录或模型。

创建响应应包含 `execution_profile: app_role` 和真实会话 ID。未返回该标识时提示升级宿主，不继续按普通助手发送角色对话。`role_instructions` 要求 1–12000 字符，创建后固定；改变角色规则时新建会话，不在线覆盖历史。

后续通过 `chat.send` 发送玩家文本或场景 JSON，无需重复人格。`chat.final` 才是最终答案。每个角色复用自己的会话，宿主保留该会话最近完整对话；模型输入上限为 48000 字符，超出时移除最旧对话对，不修改持久化历史。系统规则和当前输入超限时明确报错。默认模型与既有模型选择机制一致。

基础系统提示说明角色助手身份，要求遵循应用规则、将玩家与场景视为输入数据、如实表达能力，并遵循应用的语言和输出格式。角色没有宿主工具，不能访问项目文件、私人记忆或其他 NPC 的对话。主从形态下主 NPC 的提示词附成员名册与咨询规则，成员基座提示强调只回答主 NPC 的咨询。

旧版轻量咨询的流式事件在既有 `chat.delta`/`chat.final` 之外新增 `npc.member`（`phase: started | finished`，含成员名、成员会话 ID 与回答摘要）：应用可选择渲染"从 NPC 插话"或忽略；成员不发送增量文本。咨询的每一问/答都写入成员自己的会话历史。

支持流式输出、用量事件、取消、分叉和删除；分叉主会话会级联创建全新成员会话（成员历史不复制），单独分叉成员则脱离主从成为独立 NPC 会话。轻量咨询不支持宿主工具装备、暂停/继续、cron、永续记忆及显式增强执行。未指定 `execution_profile` 的会话保持原行为。

部署新宿主后通过应用正常的新会话流程启用。旧 NPC 不自动迁移；应用重启如果会清理旧 NPC，原对话可能被删除，按应用既有退出和新局规则处理。

咨询前宿主校验成员仍属于当前主会话且可执行；成员缺失、删除或改绑时返回咨询错误，不创建普通助手。`npc.member` 的结构化字段经 Gateway 完整透传，并携带主请求的 `request_id`。
