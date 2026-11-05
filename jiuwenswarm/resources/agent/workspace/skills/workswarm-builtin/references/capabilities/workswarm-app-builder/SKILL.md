---
name: workswarm-app-builder
description: |
  用于开发可运行的本地网页应用、小游戏或 AI 交互工具，以及将应用上架本机应用市场。
  用户说"帮我开发一个五子棋小游戏，支持AI对战"等应用开发需求时自动匹配，无需提及技能名、WorkSwarm 或上架。
  仅讨论游戏规则、算法或问答而未要求开发应用时不触发。
---

# WorkSwarm 应用插件

适用于 code 模式。先遵循 [内置能力总目录](../../../SKILL.md) 的模式与发布授权约束；
选择总目录不等于主动选择本能力。非 code 模式先澄清需求并提示切换，不直接开发或发布。

将应用的游戏规则与结构化状态留在页面，将 AI 决策交给 WorkSwarm。应用只使用同源客户端，不读取实例令牌或宿主凭据。

## 触发与上架方式

两种方式使用相同的自然语言需求（如"帮我开发一个五子棋小游戏，支持AI对战"），不要求用户提及技能名、WorkSwarm 或应用市场。

- **主动选择技能**：按开发与上架流程完成应用，功能验证通过后自动执行本机市场发布。用户明确要求暂不上架时只开发，不发布。
- **未主动选择技能**：开发和验证完成后，在现有对话中展示应用名称、简介、图标及所需 `ai` 能力，提示是否上架；确认后执行发布。
- 无法确定用户是否主动选择了技能时，发布前先在对话中确认。仅讨论或修复已有应用不视为授权发布；不新增聊天弹框、产物扫描或会话级"已上架"标记。

## 接入边界与接口

应用市场只负责上架、端口分配、启停和状态接收。会话、AI 请求、存档和重试属于应用自身；数据面直接访问 Gateway，不调用 `app_market.*` 转发消息，不依赖市场的角色记忆、摘要或压缩。

- 市场启动时注入 `WORKSWARM_APP_HOST/PORT/URL`、`WORKSWARM_GATEWAY_HOST/PORT/URL` 和 `WORKSWARM_GATEWAY_WEB_PORT/API_URL`。`GATEWAY_URL` 为通用 `/tui` WebSocket，`API_URL` 为 Web Channel `/ws` WebSocket；不是 HTTP 模型 API。缺少地址直接提示“未配置 WorkSwarm Gateway 访问地址”，禁止猜测端口或回退 bridge。
- 应用监听分配端口；需要服务端接入时，把服务、客户端适配及依赖放在应用自己的目录，通过应用同源接口向页面提供业务能力。市场静态托管仅提供文件和状态检查，不提供 AI 客户端。可在应用 `scripts/app_server.py` 实现独立服务；当前静态启动模板发现此文件时运行它，否则使用纯静态托管。
- 服务端使用现有 Gateway WebSocket 协议：先等待 `connection.ack`，发送 `{type:"req", id, method, params}`，按同一 `id` 匹配 `res`；失败读取 `ok/error/code`。沿用现有身份认证，不使用状态令牌冒充 Gateway 身份，不把凭据、任意 RPC 或文件接口暴露给页面。

| 操作 | 既有接口及参数 | 约束 |
|---|---|---|
| 优雅停止 | 应用自身 `POST /_workswarm/shutdown`，请求头 `Authorization: Bearer <WORKSWARM_STATUS_TOKEN>` | 自带 `scripts/app_server.py` 的应用必须实现：校验令牌后清理角色会话（中断、删除并确认）再退出；响应 200 后尽快退出。市场停止时先调用它并最多等待 8 秒，未实现或超时才强杀，强杀不会触发任何退出处理。纯静态托管由市场模板内置 |
| 页面来源与缓存 | Web 使用应用专属的 `*.localhost` 主机，桌面使用 `127.0.0.1`。应用服务端所有响应带 `Cache-Control: no-store` | 自带服务器的 Host 校验必须按端口放行 `127.0.0.1`、`localhost` 及任意 `*.localhost`（市场 Web 入口为 `workswarm-app-<摘要>.localhost:<port>`），页面通过同源路径加载资源和调用应用接口。`WORKSWARM_APP_URL` 是本地服务地址，页面从实际浏览器来源构造 URL。纯静态托管由模板内置 |
| 创建 | Web `/ws`：`session.create`，`create_token`、`persist_session:false`、`mode:"agent"`、`project_id`、`work_mode`、`title` | 项目与工作模式继承创建应用的会话；纯对话角色指定 `execution_profile:"app_role"` 和 `role_instructions`；保存真实 `session_id`；不指定模型 |
| 检查 | Web：`session.get_metadata`，`session_id` | 仅明确 `NOT_FOUND/SESSION_NOT_FOUND` 才补建；断连、超时、鉴权错误不代表会话丢失 |
| 消息 | Web：`chat.send`，`session_id`、`query/content`、`mode/work_mode`，`is_stream:true` | accepted 不是答案；匹配 session 和 request ID 的 `chat.final`，忽略旧请求重放和 keepalive；错误、审批或提问明确处理 |
| 中断 | Web：`chat.interrupt`，`session_id`、`wait_for_stop:true` | 超时或重开时停止旧回合，丢弃迟到响应 |
| 清空 | 通用 `/tui`：`session.rewind`，`session_id`、`turn_index:1` | 当前 Web Channel 未提供此方法；不修改核心接口，不做不支持的订制 |
| 删除 | Web：`session.delete`，`session_ids:[...]` | 删除可能异步，查询确认不存在后才移除本地绑定；清理失败保留待处理记录 |

## 会话与请求规范

- 纯文字 NPC、棋手等无需工具的角色使用 `execution_profile:"app_role"`。创建时将人格、业务规则、输出 schema 写入 `role_instructions`（1–12000 字符），后续聊天仅发送当前业务输入。宿主保留精简基础系统提示；不加载项目指令、个人/项目记忆、技能和工具。创建响应必须确认 `execution_profile:"app_role"`；旧宿主不支持时明确提示升级，不降级到普通助手。需要工具的角色继续使用普通会话。轻量角色仅支持文字对话和取消，不支持暂停/继续、增强执行、永续记忆、cron 或装备工具；保留自身对话历史。
- 角色会话必须绑定创建应用的项目。市场通过 `WORKSWARM_SOURCE_SESSION` 注入发布清单的来源会话 ID；应用服务端调用 `session.get_metadata` 读取其 `project_id` 和 `work_mode`，创建角色会话时透传这两个字段。不要以应用子目录代替项目路径，也不要固定 `work_mode:"work"`；消息请求沿用角色会话的工作模式。旧市场未注入时，可从应用自身配置读取发布时保存的来源会话 ID。来源缺失或查询失败时明确报错，不静默建到默认项目；真正的默认项目沿用来源的默认项目 ID 与工作模式。
- 每用户、每应用、每个稳定 AI 角色最多一个活动会话；N 个角色最多 N 个会话。应用保存绑定和幂等创建 token，初始化串行执行，多标签页不能并发建会话或请求同一角色。不使用时间戳或局号作为 role_id。
- 默认 `persist_session:false`，仍保留普通多轮历史；该参数不是聊天历史保存开关。不要为保存对局开启永续记忆，不修改元数据绕过创建配置。
- 独立对局默认新局重置：清空本地棋盘，检查并复用角色会话，中断并 rewind 历史，成功后才启用操作；缺失才创建新会话。新局首轮重新初始化，禁止旧棋盘搭配新会话。
- 应用负责优雅退出和会话清理，市场只终止进程；在 `/_workswarm/shutdown` 处理或进程退出处理里中断、删除并确认，不依赖页面 unload。市场请求优雅停止后最多等待 8 秒，超时强杀且不触发退出处理，因此清理逻辑要在此窗口内完成。强杀或断网后保留绑定，下一次启动先完成待清理任务。不要按标题批量删除普通会话。
- 首轮一次写清角色、规则、初始状态、后续增量输入和输出 schema，以及重复操作的语义。后续省略 instruction，只发 `{"state_delta":{"last_move":[row,col,color]}}` 这类短增量；WorkSwarm 保留历史，不重发规则、棋盘或历史记录。
- 超时重试复用同一会话，只重发当前操作增量；即使首轮超时也不重发全量或 reset。首轮明确重复 last_move 是同一次操作重试，不能重复落子；已完成时返回同一结果。更复杂业务使用应用自己的操作标识和幂等校验，不能只依赖模型口头承诺。
- 应用自行设置有限的端到端等待时间和重试次数，考虑供应商限流退避，避免短时间重复发送、叠加重试。显示等待时长和错误，并允许重开/取消；不要把 keepalive 当作答案。不更改 WorkSwarm 通用重试策略。
- 默认模型交给 WorkSwarm；不硬编码模型、provider、API 地址、`max_tokens` 或关闭思考参数。难度通过首轮决策要求区分，不承诺仅凭提示词保证低时延。
- 返回内容必须校验 JSON/schema、范围、合法性及请求序号；每次仅应用一次仍有效结果。存档优先保存结构化状态，不要求模型恢复记忆；无跨局记忆需求时不触发总结调用。

上架前验证：同角色连续新局至少5次、刷新至少3次无会话增长；真实多轮 AI、首轮与增量超时重试、重复操作、迟到响应、断连、手动删会话、停止及重新启动均保持状态一致；确认停止后会话删除或有明确待清理记录。核对链路不经过 `/app-market/capabilities` 或市场内部会话模块。

## 展示质量

上架前必须生成应用专属图标、符合用户语言习惯的名称和完整简介，并让用户预览。复用应用市场现有卡片，不修改聊天界面、技能广场或另写卡片组件。

- **图标**：根据应用核心用途原创设计（简单图形用 SVG，插画生成 PNG/WebP），提供至少 128×128 方形画布，32px 与明暗背景下仍可辨识。禁止首字母、纯文字、外链占位图或复制品牌图标。
- **名称**：优先用户当前语言并保留有助识别的另一语言，如"五子棋 · Gomoku"。不使用文件名或技术 ID。
- **简介**：用户语言 2–3 句（中文约 60–120 字，英文约 35–70 词），覆盖玩法、特色、AI 如何使用 WorkSwarm、存档行为；只描述已实现能力。
- **元信息**：入口 `<head>` 写入 `<title>`、`<meta name="application-name">`、`<meta name="description">`、`<link rel="icon" href="assets/app-icon.svg">`。图标必须是应用目录内的本地文件。
- **验收**：核对市场实际展示的名称、简介与图标；图片可加载、无占位字母、文案与功能一致。

## 上架流程

1. 按展示质量规范生成应用页面、图标、版本、名称和简介，写入标准 HTML 元信息。
2. 接入应用自己的 Gateway 客户端，按会话管理规范验证数量上限、复用、重置及退出清理，并完成真实 AI 请求；需要存档时另测保存与恢复。
3. 在现有对话中展示应用及所需 `ai` 能力；主动选择技能时验证通过后自动发布，自动匹配时提示并等待确认。验证失败不得登记。
4. 确认后执行本 Skill 的发布脚本（复用 WorkSwarm GatewayClient 调用 `app_market.publish`，不直接改市场数据）：

   ```bash
   {python} "{skill_directory}/scripts/publish.py" --entry <入口绝对路径> --session-id <当前会话ID> --gateway-url <实际 WebChannel WebSocket 地址> --confirmed
   ```

   地址从当前运行配置取得，不猜测端口，不使用通用 TUI 端口替代 WebChannel。
5. 以返回的 `app_id` 报告结果，进入市场核对卡片。页面需要市场注入运行配置；直接双击页面应提示需要从应用市场启动。
