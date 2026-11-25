---
name: workswarm-app-builder
description: |
  用于开发可运行的本地网页应用、小游戏或 AI 交互工具，以及将应用上架本机应用市场。
  用户说"帮我开发一个五子棋小游戏，支持AI对战"等应用开发需求时自动匹配，无需提及技能名、WorkSwarm 或上架。
  支持在应用中接入单角色 NPC，或接入由多个角色分别发言的 Team NPC。
  仅讨论游戏规则、算法或问答而未要求开发应用时不触发。
---

# WorkSwarm 应用插件

适用于 code 模式。先遵循 [内置能力总目录](../../../SKILL.md) 的模式与发布授权约束；
选择总目录不等于主动选择本能力。非 code 模式先澄清需求并提示切换，不直接开发或发布。

将游戏规则和结构化状态交由应用管理，将 AI 决策交给 WorkSwarm。页面只调用应用的同源接口。应用服务端使用实例状态令牌处理就绪和停止请求，Gateway 身份按部署环境配置。

## 触发与上架方式

两种方式使用相同的自然语言需求（如"帮我开发一个五子棋小游戏，支持AI对战"），不要求用户提及技能名、WorkSwarm 或应用市场。

- **主动选择技能**：按开发与上架流程完成应用，功能验证通过后自动执行本机市场发布。用户明确要求暂不上架时只开发，不发布。
- **未主动选择技能**：开发和验证完成后，在现有对话中展示应用名称、简介、图标及所需 `ai` 能力，提示是否上架；确认后执行发布。
- 无法确定用户是否主动选择了技能时，发布前先在对话中确认。仅讨论或修复已有应用不视为授权发布；不新增聊天弹框、产物扫描或会话级"已上架"标记。

## 接入边界与接口

应用市场只负责上架、端口分配、启停和状态接收。会话、AI 请求、存档和重试属于应用自身；数据面直接访问 Gateway，不调用 `app_market.*` 转发消息，不依赖市场的角色记忆、摘要或压缩。

- 市场启动时注入 `WORKSWARM_APP_HOST/PORT/URL`、`WORKSWARM_GATEWAY_HOST/PORT/URL` 和 `WORKSWARM_GATEWAY_WEB_PORT/API_URL`。`GATEWAY_URL` 为通用 `/tui` WebSocket，`API_URL` 为 Web Channel `/ws` WebSocket；不是 HTTP 模型 API。缺少地址直接提示“未配置 WorkSwarm Gateway 访问地址”，禁止猜测端口或回退 bridge。
- 启动模板发现 `scripts/app_server.py` 时运行该脚本，否则使用纯静态托管。自带服务端时监听分配地址和端口，实现就绪与停止接口。服务、Gateway 客户端和依赖放在应用目录，页面通过同源业务接口访问。接入单角色 AI 时读取 [单角色 AI 接入示例](references/single-npc.md)。
- 服务端使用现有 Gateway WebSocket 协议：先等待 `connection.ack`，发送 `{type:"req", id, method, params}`，按同一 `id` 匹配 `res`；失败读取 `ok/error/code`。沿用现有身份认证，不使用状态令牌冒充 Gateway 身份，不把凭据、任意 RPC 或文件接口暴露给页面。

| 操作 | 既有接口及参数 | 约束 |
|---|---|---|
| 就绪 | 应用自身 `GET /_workswarm/status`，请求头 `Authorization: Bearer <WORKSWARM_STATUS_TOKEN>` | 准备就绪后返回 200 和 `app_id`、`instance_id`、`seq`、`state:"running"`。实例标识从 `WORKSWARM_INSTANCE_CONTEXT` 指向的文件读取。纯静态托管由模板内置 |
| 优雅停止 | 应用自身 `POST /_workswarm/shutdown`，使用同一状态令牌 | 市场等待 HTTP 200 最多 2 秒，收到后再等主进程退出最多 8 秒。应用及时响应，随后中断并删除绑定会话，再退出。失败或超时进入市场的停止回退流程。纯静态托管由模板内置 |
| 页面来源与缓存 | Web 使用应用专属的 `*.localhost` 主机，桌面使用 `127.0.0.1`。应用服务端所有响应带 `Cache-Control: no-store` | 自带服务器的 Host 校验必须按端口放行 `127.0.0.1`、`localhost` 及任意 `*.localhost`（市场 Web 入口为 `workswarm-app-<摘要>.localhost:<port>`），页面通过同源路径加载资源和调用应用接口。`WORKSWARM_APP_URL` 是本地服务地址，页面从实际浏览器来源构造 URL。纯静态托管由模板内置 |
| 创建 | Web `/ws`：`session.create`，`create_token`、`persist_session:false`、`project_id`、`work_mode`、`title` | `app_role` 创建响应需确认执行配置、真实 `session_id` 与 `mode`。默认执行配置的 `npc_session:true` 响应可能省略 `mode`，需要实际模式时用 `session.get_metadata` 查询 |
| 检查 | Web：`session.get_metadata`，`session_id` | 仅明确 `NOT_FOUND/SESSION_NOT_FOUND` 才补建；断连、超时、鉴权错误不代表会话丢失 |
| 消息 | Web：`chat.send`，请求帧顶层 `is_stream:true`，`params` 传 `session_id`、字符串 `query`、`mode`、`work_mode` | `accepted` 表示受理。单角色等待 `chat.final` 或 `chat.error`；团队按 `turn_request_id` 等待对应回合结束。明确处理错误、审批或提问 |
| 中断 | Web：`chat.interrupt`，`session_id`、`intent:"cancel"` | 普通 Web 取消路径不透传 `wait_for_stop`。确认旧回合停止后再重试或重开，按会话和请求丢弃迟到响应 |
| 清空 | `session.rewind`，`session_id`、`turn_index:1` | `/tui` 有明确入口，当前 Web 默认透传可用，严格转发配置可能禁用。仅 `ok:true` 且 `rewind_context:true` 时按清空成功复用；需要干净上下文时可中断、删除并新建 |
| 删除 | Web：`session.delete`，`session_ids:[...]` | 删除可能异步，查询确认不存在后才移除本地绑定；清理失败保留待处理记录 |

## 会话与请求规范

- 纯文字 NPC、棋手等无需工具的角色使用 `execution_profile:"app_role"`。创建时将人格、业务规则、输出 schema 写入 `role_instructions`（1–12000 字符），后续聊天仅发送当前业务输入。宿主保留精简基础系统提示，不加载项目指令、个人/项目记忆、技能和工具。创建响应必须确认 `execution_profile:"app_role"`，旧宿主不支持时提示升级。需要工具的角色使用默认执行配置，并在创建时传入 `npc_session:true`。NPC 会话在 Swarm 中只读，玩家消息从应用内发送。轻量角色保留自身对话历史，支持文字对话和取消。
- 根据玩法决定角色拓扑。单个 NPC 或几个互不协作的 NPC 使用各自的单角色会话。同一轮需要多个有身份的角色协作并各自发言时，使用原生 Team NPC。开始实现团队玩法前，读取 [Team NPC 接入指南](references/team-npc.md)，按示例完成角色名册、应用服务端 Gateway 客户端、回合事件处理和前端渲染。玩家只向 leader 提交输入，应用按角色逐条展示最终发言。
- 市场应用启动前需要可用项目，Hub 安装时尝试绑定，缺失时在启动前补齐。创建角色会话时成对使用 `WORKSWARM_APP_PROJECT_ID` 和 `WORKSWARM_APP_WORK_MODE`。兼容旧宿主时，本地应用可通过 `WORKSWARM_SOURCE_SESSION` 调用 `session.get_metadata` 查询项目和工作模式。消息请求沿用该工作模式。无法确定项目和模式时明确报错。
- 每用户、每应用、每个独立角色或团队最多一个活动的应用绑定；团队成员会话由 Team 管理。应用保存绑定和幂等创建 token，初始化串行执行，多标签页不能并发建会话或提交同一轮。不使用时间戳或局号作为 role_id。
- 默认 `persist_session:false`，仍保留普通多轮历史；该参数不是聊天历史保存开关。不要为保存对局开启永续记忆，不修改元数据绕过创建配置。
- 独立对局默认新局重置。单角色中断后可 rewind 并复用；团队 NPC 需要清除上下文时删除旧 Team 会话并重新创建，清理确认后才启用新局。新局首轮重新初始化，禁止旧棋盘搭配新会话。
- 应用响应优雅停止后在退出窗口内中断、删除并确认所持有的会话，页面卸载不触发清理。强杀或断网时保留待清理绑定，下次启动先处理。只清理应用自己保存的会话 ID。
- `app_role` 的角色、固定规则和输出格式写入创建参数 `role_instructions`。首轮 `query` 包含当前操作和必要的初始状态，后续发送短增量，例如将 `{"state_delta":{"last_move":[1,2,"black"]}}` 序列化为 `query` 字符串。
- 超时先确认旧回合的受理和完成状态。按同一业务操作 ID 去重，传输重试使用新请求 ID。首次初始化未确认时，重试保留所需的初始状态。团队回合按 [Team NPC 接入指南](references/team-npc.md) 处理。
- 应用自行设置有限的端到端等待时间和重试次数，考虑供应商限流退避，避免短时间重复发送、叠加重试。显示等待时长和错误，并允许重开/取消；不要把 keepalive 当作答案。不更改 WorkSwarm 通用重试策略。
- 默认模型交给 WorkSwarm；不硬编码模型、provider、API 地址、`max_tokens` 或关闭思考参数。难度通过首轮决策要求区分，不承诺仅凭提示词保证低时延。
- 返回内容必须校验 JSON/schema、范围、合法性及请求序号；每次仅应用一次仍有效结果。存档优先保存结构化状态，不要求模型恢复记忆；无跨局记忆需求时不触发总结调用。

## AI 角色清单

新开发的运行时 AI 应用在入口 HTML 所在目录提供 `app-ai-roles.json`，按 [AI 角色清单](references/ai-roles.md) 声明角色名称、简介、可选头像和会话组的实际生命周期。市场详情展示名称、头像和会话说明，发布链路据此登记 `ai` 能力。纯静态应用可省略清单。

## 展示质量

上架前必须生成应用专属图标、符合用户语言习惯的名称和完整简介，并让用户预览。复用应用市场现有卡片，不修改聊天界面、技能广场或另写卡片组件。

- **图标**：根据应用核心用途原创设计，统一输出 PNG（Hub 只接受 PNG；本地也使用同一张图），提供 256×256 方形画布，32px 与明暗背景下仍可辨识。禁止首字母、纯文字、外链占位图或复制品牌图标。
- **名称**：优先用户当前语言并保留有助识别的另一语言，如"五子棋 · Gomoku"。不使用文件名或技术 ID。
- **简介**：用户语言 2–3 句（中文约 60–120 字，英文约 35–70 词），覆盖玩法、特色、AI 如何使用 WorkSwarm、存档行为；只描述已实现能力。
- **元信息**：入口 `<head>` 写入 `<title>`、`<meta name="application-name">`、`<meta name="description">`、`<meta name="application-version" content="1.0.0">`、`<link rel="icon" href="assets/app-icon.png">`。图标必须是应用目录内的本地 PNG 文件。版本由 1–4 段数字组成，可加 `-` 或 `+` 引导的后缀。后缀长度 1–16，仅含英文字母、数字、点或连字符，总长最多 32。用户可见功能变化时更新版本。
- **验收**：核对市场实际展示的名称、简介、版本与图标；图片可加载、无占位字母、文案与功能一致。

## 上架流程

1. 按展示质量规范生成应用页面、图标、版本、名称和简介，写入标准 HTML 元信息。
2. 所有应用验证市场启动、页面和停止。AI 应用生成角色清单并完成真实多轮请求，覆盖首轮和后续超时、重复操作、迟到响应、连接中断、会话丢失、新局重置和停止清理。独立对局同角色连续新局至少 5 次，刷新至少 3 次，核对会话数量和清单说明。Team 另验角色发言归属和回合收口，需要存档时另验保存恢复。
3. 在现有对话中展示应用及声明的 AI 角色和会话策略。主动选择技能时验证通过后自动发布，自动匹配时提示并等待确认。验证失败不得登记。
4. 确认后执行本 Skill 的发布脚本（使用随 Electron 提供的 Node.js，通过 WebChannel 调用 `app_market.publish`，不直接改市场数据）：

   ```bash
   node "{skill_directory}/scripts/publish.mjs" --entry <入口绝对路径> --session-id <当前会话ID> --gateway-url <实际 WebChannel WebSocket 地址> [--version <版本>] --confirmed
   ```

   可用 `--version` 指定版本。页面已声明 `<meta name="application-version">` 时，两者须一致。未指定时使用页面版本；两者都没有时保留上次发布版本，首次发布默认 `1.0.0`。重复发布不会静默重置版本。优先读取当前会话的 WORKSWARM_GATEWAY_API_URL；否则从当前 Electron 实例的运行配置取得实际 WebChannel 地址。勿使用 AgentServer 或 TUI 通道。发布脚本先验证市场列表，再发送发布请求。
5. 以返回的 `app_id` 与 `version` 报告结果，进入市场核对卡片。页面需要市场注入运行配置；直接双击页面应提示需要从应用市场启动。
