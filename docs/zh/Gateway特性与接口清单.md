# Gateway 侧特性与接口清单

> 参考《JiuwenSwarm 特性列表》格式整理。范围：`jiuwenclaw/app_gateway.py`、`jiuwenclaw/gateway/`、`jiuwenclaw/channel/`、`jiuwenclaw/app_web_handlers.py`、`jiuwenclaw/agentserver/gateway_push/`，以及企业版 `packages/jiuwenclaw-ee/gateway/`。与 `Gateway特性与接口清单.tsv` 内容一致（TSV 可直接粘贴进 Excel）。
> 接口形态说明：Gateway 对客户端（Web/ACP/TUI/IM）暴露 WS RPC（`type=req/res/event` 帧，方法名为 `method` 字段）；对 AgentServer 以 E2A 信封（`req_method`）发起 RPC；对 Manager（EE）为专用 WS 帧协议。

| 功能 | 子功能 | 功能编号 | 功能简要描述 | 功能规格 | 对应接口 | 企业级分支额外添加（仅限：主备容灾 / Web Server 文件下载 / Gateway 优雅终止） |
|------|------|------|------|------|------|------|
| **部署形态与端口** | 进程入口 |  | Gateway 独立进程入口（拆分部署），装配 MessageHandler、ChannelManager、各频道、Cron、心跳、选主后连接远端 AgentServer | 启动参数 --agent-server-url/-H/-p/--web-path；连接重试 AGENT_CONNECT_RETRY（默认 20 次 × 3s） | `jiuwenclaw-gateway` CLI 入口; 参数: `--agent-server-url`, `--host`, `--port`, `--web-path` | ❌ |
|  | Web WS 端口 |  | 浏览器前端入站 WebSocket 服务 | 默认 ws://127.0.0.1:19000/ws（WEB_HOST/WEB_PORT/WEB_PATH） | `ws://{WEB_HOST}:{WEB_PORT}{WEB_PATH}`（默认 `ws://127.0.0.1:19000/ws`） | ❌ |
|  | Gateway 多路 WS 端口 |  | 统一 WS 服务，按路径路由 /acp（ACP JSON-RPC）与 /tui（CLI/TUI） | 默认 ws://127.0.0.1:19001（GATEWAY_HOST/GATEWAY_PORT）；ping_interval=20、ping_timeout=600 | `ws://{GATEWAY_HOST}:{GATEWAY_PORT}/acp`、`/tui`（默认端口 19001） | ❌ |
|  | VibeSkill 端口 |  | VibeSkill 独立 HTTP+WS 服务（会话/文件/VCS REST） | VIBESKILL_ENABLED（默认开）；HTTP 19002、WS 19003 | HTTP `:19002` / WS `:19003`; env: `VIBESKILL_ENABLED`, `VIBESKILL_HTTP_PORT`, `VIBESKILL_WS_PORT` | ❌（企业分支新增，属通用频道能力） |
|  | AgentServer 出站连接 |  | Gateway 作为 WS 客户端连接 AgentServer，多路复用全部 RPC 与推送 | 默认 ws://127.0.0.1:18092（AGENT_SERVER_URL/AGENT_SERVER_HOST/PORT） | env: `AGENT_SERVER_URL`（默认 `ws://127.0.0.1:18092`） | ❌ |
|  | 部署模式 |  | standalone（单机）与 active-standby（主备，Redis 选主 + 共享存储） | config.yaml: gateway.deployment_mode；gateway.instance_id 隔离资源 | config: `gateway.deployment_mode=standalone\|active-standby`, `gateway.instance_id` | ✅ 主备容灾：active-standby 模式总开关 + instance_id 资源隔离 |
| **接口-连接与握手** | 客户端连接就绪 |  | 客户端连上 Gateway WS 后首帧下发就绪事件 | payload：protocol_version=1.0、transport=<channel_id> | `type=event, event=connection.ack`; payload: `protocol_version`, `transport` | ❌ |
|  | Gateway→Agent 握手鉴权 |  | 建连时携带链路令牌头，并校验 Agent 回送的 connection.ack.link_token（Ed25519 + TOFU 指纹固定） | CLAW_LINK_AUTH_MODE=off/observe/enforce（默认 off）；CLAW_LINK_TOKEN_TTL 默认 300s | 握手头 `X-Claw-Link-Token`; env: `CLAW_LINK_AUTH_MODE`, `CLAW_LINK_TOKEN_TTL` | ❌（企业分支新增，属链路安全加固特性） |
|  | 连接状态查询 |  | Web 前端查询 Gateway↔Agent 连接状态 | — | `method=connection.status` | ❌（企业分支新增，属运维查询） |
|  | 企业 Web 上行 |  | 企业版不本地监听，反向连 Web Pod /gateway；主备下仅 PRIMARY 建立 uplink | ENTERPRISE_WEB_ENABLED、ENTERPRISE_WEB_GATEWAY_URL | env: `ENTERPRISE_WEB_ENABLED`, `ENTERPRISE_WEB_GATEWAY_URL`; `method=web.connection_ack`; params: `conn_id` | ❌（企业分支新增，属企业 Web 接入特性；仅 PRIMARY 连接部分见「单活联动」） |
| **接口-转发 RPC（Gateway→Agent 透传）** | 对话/会话/技能等转发 |  | initialize、session.create、acp.tool_response、chat.send/interrupt/resume/user_answer、history.get、browser.start、skills.* 全族、extensions.*、skilldev.*、tools.add 经规范化后透传 AgentServer | chat.resume 归一为 chat.interrupt+intent=resume；chat.send/history.get 强制流式 | `method=initialize` / `session.create` / `acp.tool_response` / `chat.send\|interrupt\|resume\|user_answer` / `history.get` / `browser.start` / `skills.*` / `extensions.*` / `skilldev.*` / `tools.add` | ❌ |
|  | remote 会话转发 |  | gateway.web_session_storage=remote 时 session.create/delete 转发 Agent，session.list 改由网关索引本地应答 | 动态注册 forward 方法 | config: `gateway.web_session_storage=remote`; `method=session.create\|delete\|list` | ✅ 主备容灾：会话不落单机磁盘，主备切换后 Web 会话可续 |
| **接口-Web 本地 RPC：配置** | 通用配置读写 |  | 读/写模型（default/video/audio/vision）、embedding、搜索 key、deepsearch 等配置，写 .env（敏感项加密）+ config.yaml，并触发热同步 | 键表 _CONFIG_SET_ENV_MAP（30+ 项）+ _CONFIG_YAML_KEYS | `method=config.get` / `config.set`; params: `model`, `api_key`, `api_base` 等 30+ 键 | ❌ |
|  | 模型校验与多模型管理 |  | 校验模型连通性；模型列表的增删、校验、激活 | — | `method=config.validate_model`; `method=models.list\|save\|remove\|validate\|set_active` | ❌ |
|  | 配置热同步到 Agent |  | 配置保存后向 Agent 发 agent.reload_config（全量 config + 增量 env）；涉及模型 env 时追发 browser.runtime_restart | 启动时可自动同步（SYNC_CONFIG_ON_STARTUP 默认开）；发送前解密扩展敏感配置 | `method=agent.reload_config`; params: `config`, `env`; `method=browser.runtime_restart` | ❌ |
|  | 语言/路径 |  | 界面语言读写；工作区路径读写 | — | `method=locale.get_conf\|set_conf`; `method=path.get\|set` | ❌ |
| **接口-Web 本地 RPC：会话与历史** | 会话管理（local 模式） |  | Gateway 本地扫描 sessions 目录，实现会话列表/创建/删除 | remote 模式下改走转发 + 网关索引 | `method=session.list\|create\|delete`; params: `session_id` | ❌ |
|  | 聊天入口 |  | Web 端 chat 系列本地 handler（转发 + ack 编排） | — | `method=chat.send\|resume\|interrupt\|user_answer`; `method=history.get` | ❌ |
| **接口-Web 本地 RPC：记忆** | 记忆检索 |  | 调 memory 计算/检索 | — | `method=memory.compute` | ❌ |
|  | 记忆文件只读浏览 |  | 列出/读取 MEMORY.md 与 memory/*.md（防目录穿越，分页行读取） | 白名单路径校验；from_line/lines 分页 | `method=memory.list`; `method=memory.get`; params: `path`, `from_line`, `lines` | ❌（企业分支新增，属记忆查看能力） |
|  | 记忆禁区配置 |  | 读写 memory.forbidden_memory_definition | — | `method=memory.forbidden.get\|set` | ❌ |
| **接口-Web 本地 RPC：权限** | 权限配置代理 |  | 14 个 permissions.* 方法经 E2A 转发 AgentServer；Agent 未就绪时降级本地执行（与 config_rpc 同源） | 双路径容灾 | `method=permissions.enabled.get\|set`; `permissions.tools.get\|set\|update\|delete`; `permissions.rules.get\|create\|update\|delete`; `permissions.approval_overrides.get\|delete`; `permissions.file_guard.workspace.rw_enabled.get\|set` | ❌（approval_overrides/file_guard 为企业分支新增，属权限特性） |
|  | 数字分身权限作用域 |  | 按 channel_id+user_id 维度的 owner_scopes 直写 Gateway 配置并热载权限引擎 | 不经 E2A | `method=permissions.owner_scopes.get\|set`; params: `owner_scopes`, `deny_guidance_message` | ❌ |
| **接口-Web 本地 RPC：更新器** | Windows 自动更新 |  | 更新状态查询、检查、下载、更新策略配置 | — | `method=updater.get_status\|check\|download\|get_conf\|set_conf` | ❌ |
| **消息路由主流程** | 双队列枢纽 |  | Channel 入站进 _user_messages，Agent 响应/推送进 _robot_messages，由派发循环回投各 Channel | 单例；启停 start_forwarding/stop_forwarding | 内部组件（用户/机器人双队列，无外部接口） | ❌ |
|  | E2A 规范化 |  | 入站 Message 规范化为 E2AEnvelope（method/params/session_id/channel/is_stream 等）发往 Agent | 协议版本 1.0；规范见 docs/zh/E2A-protocol.md | E2A 信封字段: `method`, `params`, `session_id`, `channel`, `is_stream`（协议版本 1.0） | ❌ |
|  | 非流式 RPC |  | 一元请求-响应，按 request_id 对齐，60s 超时；非 chat 类可并行 | — | E2A 请求 `is_stream=false`; 按 `request_id` 对齐一元响应 | ❌ |
|  | 流式 RPC |  | 多 chunk 流式接收直至 is_complete，chunk 转 event Message 逐帧回投 | 流结束后 0.7s trailing grace | E2A 请求 `is_stream=true`; 多 chunk 直至 `is_complete` | ❌ |
|  | 中断/暂停/恢复/补充 |  | chat.interrupt 按 intent 分流：cancel/pause/resume/supplement（补充输入可带附件） | params.intent、new_input、files | `method=chat.interrupt`; params: `intent=cancel\|pause\|resume\|supplement`, `new_input`, `files` | ❌ |
|  | 服务端推送旁路 |  | Agent 主动 push（wire 打 _jiuwenclaw_server_push 标记）不进 RPC 等待队列，直接转 robot 队列或特殊处理（cron、文件下载） | — | push 帧标记 `metadata._jiuwenclaw_server_push`; `response_kind=cron` / `file.download.*` | ❌（其 file.download 分支归「下行（Agent→Gateway）」行） |
|  | 处理状态事件 |  | 流式开始/结束向前端发处理中状态 | — | `event=chat.processing_status`; payload: `is_processing` | ❌ |
|  | 断连清理 |  | TUI/WS 客户端断开时对绑定会话发 chat.interrupt 取消 | — | 内部行为：断连触发 `method=chat.interrupt` | ❌ |
| **频道接入** | 频道管理器 |  | 统一注册/注销/查找频道，出站派发循环消费 robot 队列调 Channel.send；配置变更触发频道热重建 | 配置源：config.yaml channels.* 或（主备）channel_config DB | config: `channels.*`（各频道子段） | ❌（DB 配置源部分见「DB overlay」行） |
|  | Web 前端 |  | 浏览器 WS 频道（本地）或企业 uplink | — | 同 Web WS 端口（`/ws`）或企业 uplink | ❌ |
|  | 飞书 |  | 飞书自建应用 WS 长连接（lark_oapi，无需公网回调）；支持单 bot 与多 bot（feishu:<app_id>）、群聊数字分身、消息合并 | app_id/app_secret；message_merge_window_ms 默认 15000 | config: `channels.feishu.*`; params: `app_id`, `app_secret`, `encrypt_key`, `verification_token`; 多 bot: `channels.feishu.<bot_key>.*` | ❌ |
|  | 小艺 |  | 华为小艺 A2A 双 WS 接入（xiaoyi_channel/xiaoyi_claw 两种模式），AK/SK 鉴权 | ak/sk/agent_id；OBS 文件上传 | config: `channels.xiaoyi.*`; params: `ak`, `sk`, `agent_id`, `mode`, `ws_url1/2` | ❌ |
|  | 钉钉 |  | 钉钉 Stream 模式机器人 | client_id/client_secret | config: `channels.dingtalk.*`; params: `client_id`, `client_secret`, `allow_from` | ❌ |
|  | Telegram |  | Bot API 接入，群聊触发模式可配 | bot_token；group_chat_mode=mention/all/reply/off | config: `channels.telegram.*`; params: `bot_token`, `parse_mode`, `group_chat_mode` | ❌ |
|  | Discord |  | discord.py Bot 接入 | bot_token；可选 guild/channel 过滤、block_dm | config: `channels.discord.*`; params: `bot_token`, `guild_id`, `channel_id`, `block_dm` | ❌ |
|  | WhatsApp |  | 本地 Baileys Node Bridge ↔ Python WS | bridge_ws_url 默认 ws://127.0.0.1:19600/ws；auto_start_bridge | config: `channels.whatsapp.*`; params: `bridge_ws_url`, `auto_start_bridge`, `bridge_command` | ❌ |
|  | 企业微信 |  | 企微 AI 机器人 WS 长连接；支持数字分身 | bot_id/secret/ws_url | config: `channels.wecom.*`; params: `bot_id`, `secret`, `ws_url` | ❌ |
|  | 个人微信 |  | 微信 iLink Bot HTTP 长轮询 + 扫码登录 | auto_login、credential_file | config: `channels.wechat.*`; params: `base_url`, `bot_token`, `auto_login`, `credential_file` | ❌ |
|  | CLI/TUI |  | GatewayServer /tui 路由，安装 CLI 专属本地 handlers | channel_id=tui | WS 路径 `/tui`（req/res/event 帧协议） | ❌ |
|  | ACP |  | GatewayServer /acp 路由，ACP JSON-RPC 桥接（内外 session 映射、自动 session.create） | idle finalize 3s | WS 路径 `/acp`（ACP JSON-RPC） | ❌ |
|  | VibeSkill |  | 独立 HTTP+WS：会话 CRUD、message/abort、文件读写、find、vcs/diff/rollback/export、register-skill、summarize 等 REST | POST /api/v1/session 等；WS /api/v1/messages | REST `POST\|GET\|DELETE /api/v1/session[/{id}/message\|abort\|file\|find\|vcs\|diff\|rollback\|export\|register-skill\|summarize]`; WS `/api/v1/messages?sessionID=` | ❌（企业分支新增，属通用频道能力） |
|  | 频道配置 RPC |  | 前端按频道读写配置并热重建（8 类频道 get_conf/set_conf + 微信登录 UI/解绑） | — | `method=channel.get`; `channel.{feishu\|xiaoyi\|telegram\|dingtalk\|whatsapp\|discord\|wecom\|wechat}.get_conf\|set_conf`; `channel.wechat.get_login_ui\|unbind` | ❌ |
| **IM 管线（数字分身）** | 入站相关性过滤 |  | 群聊消息经平台适配器 + LLM 判断相关性并改写 query，决定是否转发 Agent（@提及必回） | 仅 group_digital_avatar 开启时生效 | config: `channels.*.group_digital_avatar`, `my_user_id`, `bot_name` | ❌ |
|  | 出站群发/私发路由 |  | LLM 分类 + 关键词兜底判断个人行动类回复，升级为 DM（写 reply_scope=dm 等 metadata） | 跳过流式中间事件 | 出站 metadata: `reply_scope=dm`, `reply_feishu_open_id` / `reply_wecom_user_id` | ❌ |
|  | 消息合并 |  | 同一用户短时间内多条消息合并后再入 Gateway；控制消息立即 flush | 飞书/企微；窗口 message_merge_window_ms | config: `channels.feishu.message_merge_window_ms`（默认 15000） | ❌ |
| **Slash 命令** | 会话重置 |  | 受控 IM 通道（feishu/xiaoyi/dingtalk/whatsapp/wecom/wechat）重置 session | — | 消息文本 `/new_session`（支持前后 `@mention`） | ❌ |
|  | 模式切换 |  | 一级入口与直达值切换 agent/code/team 模式；模式变化取消旧任务 | /mode agent→agent.plan 等 | 消息文本 `/mode agent\|code\|team\|agent.plan\|agent.fast\|code.plan\|code.normal`; `/switch plan\|fast\|normal` | ❌ |
|  | 技能列表 |  | 整行 /skills list 拦截，调 skills.list 后以 IM 通知展示 | — | 消息文本 `/skills list` → `method=skills.list` | ❌ |
|  | 工作区浏览 |  | 全通道 /ls [path]、/view <file>，经 command.ls/view RPC 到 Agent | — | 消息文本 `/ls [path]`、`/view <file>` → `method=command.ls` / `command.view` | ❌（企业分支新增，属命令能力） |
|  | 客户端侧命令 |  | /resume（CLI 会话恢复）、/workspace_dir（TUI 工作区路径）标注为 client scope | — | `/resume` → `method=command.resume`; `/workspace_dir get\|set\|clear` → params: `workspace_dir` | ❌ |
| **会话管理** | IM 身份→会话映射 |  | 按 provider::chat::bot[::user] 身份映射/轮转 Agent session，并派生 service_id/agent_id 供分布式路由 | 作用域 gateway.session_map_scope=per_chat_bot / per_chat_bot_user | config: `gateway.session_map_scope=per_chat_bot\|per_chat_bot_user`; session_id 形态 `provider::chat::bot[::user]::ts::suffix` | ❌（service_id/agent_id 派生为企业分支新增，属多租户路由特性） |
|  | 会话映射存储 |  | standalone 存本地 session_map.json；主备存 Redis（按实例隔离，TTL 默认 7 天） | 键 session_map:{claw_id}:{identity} | 本地文件 `session_map.json`; Redis 键 `session_map:{claw_id}:{identity}`（TTL 默认 604800s） | ✅ 主备容灾：会话映射入 Redis（TTL 7 天），升主 reload 保证 IM 会话不丢 |
|  | Web 会话索引 |  | remote 模式下网关维护轻量会话预览索引，session.list 本地应答不打 Agent | 每会话预览最多 10 条；分页 limit≤200 | 本地文件 `{workspace}/gateway/session_index.json`; config: `gateway.web_session_storage` | ✅ 主备容灾：Web 会话列表不依赖单机 sessions 目录 |
|  | ACP/VibeSkill 会话别名 |  | 外部 session id ↔ 内部 id 映射，必要时自动 session.create | — | 内部映射; 必要时自动 `method=session.create` | ❌ |
| **定时任务 Cron** | 任务模型 |  | 任务含表达式/时区/推送频道/提前唤醒/单次删除/过期标记 | targets：web/feishu(:app_id)/whatsapp/wecom/xiaoyi/wechat；wake_offset_seconds 默认 60 | 字段: `id/name/enabled/cron_expr/timezone/wake_offset_seconds/description/targets/delete_after_run/expired` | ❌ |
|  | 调度表达式 |  | 5 段（分 时 日 月 周）或 7 段（+秒 +年，one-shot）；croniter 校验；过期 one-shot 拒创建 | 轮询粒度约 5s | `cron_expr`: 5 段 `分 时 日 月 周` 或 7 段 `分 时 日 月 周 秒 年`（one-shot） | ❌ |
|  | 唤醒-执行-推送 |  | 提前 wake 调 Agent 执行 description（channel_id=__cron__ 发 chat.send），到点把结果（chat.final）推 targets；未完成先占位后补发 | 单次任务执行后自动删除/置过期 | 内部: `channel_id=__cron__` 发 `method=chat.send`; 结果以 `event=chat.final` 推 `targets` | ❌（单活开关见「单活联动」行） |
|  | 存储后端 |  | standalone 用本地 JSON 文件；active-standby 用 Redis Hash（按 instance_id 隔离，不可用 fail-fast） | 文件 {workspace}/gateway/cron_jobs.json；Redis 键 gateway:cron_jobs:{instance_id} | 文件 `{workspace}/gateway/cron_jobs.json`; Redis 键 `gateway:cron_jobs:{instance_id}` | ✅ 主备容灾：Cron 任务存 Redis Hash 主备共享（Redis 不可用 fail-fast） |
|  | Web RPC |  | 前端 Cron 面板全套 CRUD/预览/立即执行 | preview count 1–50 | `method=cron.job.list\|get\|create\|update\|delete\|toggle\|preview\|run_now`; params: `id`, `name`, `cron_expr`, `timezone`, `targets` | ❌ |
|  | Agent 工具转发 |  | Agent 侧 cron 工具经 server push（cron.response）转发到网关 CronController 执行，结果以 chat.tool_result 回传 | 工具：cron_list_jobs/get/create/update/delete/toggle/preview/run_now | Agent 工具 `cron_create_job` 等 → push `response_kind=cron`; payload: `action`, `data` | ❌ |
| **心跳** | 周期探活 |  | 网关按间隔向 Agent 发心跳 chat 请求；有 workspace/HEARTBEAT.md 则驱动 Agent 执行任务清单，否则回 HEARTBEAT_OK | 间隔 HEARTBEAT_INTERVAL/heartbeat.every（代码默认 60、YAML 示例 3600）；超时 HEARTBEAT_TIMEOUT；生效时段 active_hours（支持跨午夜） | config: `heartbeat.every\|enabled\|active_hours`; env: `HEARTBEAT_INTERVAL`, `HEARTBEAT_TIMEOUT`, `HEARTBEAT_ENABLED` | ❌ |
|  | 结果回传 |  | 心跳结果以事件推到配置频道（默认 web） | — | `event=heartbeat.relay`; config: `heartbeat.target`（默认 `web`） | ❌ |
|  | 心跳配置 RPC |  | 前端读写心跳配置（写回 config.yaml 并热重启服务）、获取 HEARTBEAT.md 路径 | — | `method=heartbeat.get_conf\|set_conf\|get_path`; params: `every`, `target`, `active_hours` | ❌ |
| **选主与主备** | Redis 选主 |  | active-standby 下用 Redis SET NX EX 抢锁 + TTL 续约选出 PRIMARY | 锁 {instance_id}:gateway:leader；TTL 30s、续约 10s、竞争 5s | Redis 锁 `{instance_id}:gateway:leader`; config: `leader_election.lock_ttl_seconds\|renewal_interval_seconds\|competition_interval_seconds` | ✅ 主备容灾：核心选主，Redis SETNX+TTL，PRIMARY 失联约 30s 内自动切换 |
|  | 单活联动 |  | 角色切换回调：Cron 仅 PRIMARY 调度；SessionMap 升主重载；Manager WS 仅 PRIMARY 连接；企业 Web uplink 仅 PRIMARY；升主重载 channel_config | 心跳未做单活门控 | 内部回调（角色 `PRIMARY`/`STANDBY` 变更通知） | ✅ 主备容灾：升主联动 Cron/SessionMap/ManagerWS/企业Web/channel_config 五处切换 |
| **文件传输** | 上行（Gateway→Agent） |  | 本地文件分片（base64）发 AgentServer | chunk_size=65536、max_file_size=100MB、transfer_timeout=300s | `method=file.transfer.start\|chunk\|complete`; params: `transfer_id`, `filename`, `file_size`, `sha256`, `chunk_index`, `base64_data` | ❌（企业分支新增，但企业级部署（AGENT_RUNTIME）关闭本地上行传输） |
|  | 下行（Agent→Gateway） |  | 接收 Agent 下发文件：分片组装、SHA256 校验、落盘、过期清理 | — | `method=file.download.start\|chunk\|complete`; params: `transfer_id`, `filename`, `chunk_index`, `base64_data`, `sha256` | ✅ Web Server 文件下载：Agent 产物分片下发 Gateway 校验落盘，再经 Web 频道给用户 |
| **频道配置持久化** | DB overlay |  | 主备模式下频道配置以 channel_config DB 为唯一来源（Manager 下发写库），支持增量 patch 与全量重载 | channel_config_overlay_enabled 依据 deployment_mode | DB 表 `channel_config`; config: `gateway.deployment_mode=active-standby` | ✅ 主备容灾：频道配置持久化到共享 DB，主备双机配置一致 |
|  | 热重载回调 |  | REST/WS 写库后触发运行时频道重建（配置未变的频道不重启） | 幂等 diff 保护 | Manager 下发 section `channel_config`（op: `create\|activate\|deactivate\|delete`）→ 运行时热重建 | ✅ 主备容灾：升主/写库后热重建频道，补齐 STANDBY 窗口内的配置变更 |
| **安全** | 链路握手鉴权 |  | Manager↔Gateway、Gateway↔AgentServer 双向 Ed25519 签名令牌 + TOFU 指纹固定，防越权接入/冒充/重放 | 三档：off/observe/enforce；令牌每连接现签 | env: `CLAW_LINK_AUTH_MODE=off\|observe\|enforce`, `CLAW_LINK_TOKEN_TTL`; 握手头 `X-Claw-Link-Token` | ❌（企业分支新增，属链路安全加固特性） |
|  | 本地 RPC Hook |  | 本地 handler 前后扩展点（可改 params 与响应），带缓存清理防泄漏 | BEFORE_LOCAL_RPC_REQUEST / AFTER_LOCAL_RPC_RESPONSE | hook 事件 `BEFORE_LOCAL_RPC_REQUEST` / `AFTER_LOCAL_RPC_RESPONSE` | ❌ |
|  | 鉴权 stub |  | VibeSkill WS/HTTP 鉴权与 STS 配置目前为占位实现（恒通过/空返回） | 预留 | 预留接口 `check_ws_auth` / `check_http_auth`（当前恒通过） | ❌ |
| **EE-Manager 配置下发** | Manager WS 客户端 |  | Gateway 与 Claw Manager 的长连接：register 握手（分配 jiuwenclaw_id、交换加密/签名公钥）、心跳保活、Pod 状态上报、配置推送 | 心跳默认 60s；Pod 上报默认 30s；重连快速 3 次后转 120s probe | WS 帧 `register`/`register.ack`、`heartbeat`/`heartbeat.ack`、`pod.status.report`、`config.push`/`config.ack`; env: `GATEWAY_MANAGER_WS_URL` | ❌（企业分支新增，属企业配置下发特性；仅 PRIMARY 连接部分见「单活联动」） |
|  | config.push 路由 |  | Manager 按 section 单键下发 14 类配置（channel_config、log_masking_rule、logging/embed/task_memory/permissions_config、model/extension/skill_whitelist/service_config 模板、默认模板映射、global/service/agent 三级生效策略），处理后回 config.ack | op：create/update/delete/sync/upsert/activate 等 | 帧 `config.push`; payload: `revision`, `jiuwenclaw_id`, `config`（14 类 section）, `sig`, `enc`; 回 `config.ack` | ❌（企业分支新增，属企业配置下发特性） |
|  | 三级生效策略 |  | 请求级按 match_expr 匹配 Service→Agent 策略，缺口由 Global 补齐 template_ref 槽位（default/video/audio/vision 模型、skill_whitelist、extension_config、service_config） | priority DESC 排序；${user\|group\|bot::key} 变量 | section: `config_effective_global\|service\|agent_policies`, `config_default_template_mappings`; 表达式 `match_expr`, `${user\|group\|bot::key}` | ❌（企业分支新增，属企业配置下发/多租户路由特性） |
|  | 加签验签 |  | config.push 帧 Ed25519 验签（Encrypt-then-Sign），时间窗 + nonce + revision 单调防重放 | GATEWAY_CONFIG_VERIFY_ENABLED（默认 true）；skew 300s | 帧字段 `sig.{alg,key_id,ts,nonce,value}`; env: `GATEWAY_CONFIG_VERIFY_ENABLED\|REQUIRED`, `GATEWAY_CONFIG_SIGN_SKEW_SECONDS` | ❌（企业分支新增，属企业配置下发安全特性） |
|  | 字段级解密 |  | X25519+HKDF 解 DEK，AES-256-GCM 递归还原 ENC:v1:... 敏感字段（api_key、kubeconfig 等） | GATEWAY_CONFIG_DEC_ENABLED（默认 true） | 帧字段 `enc.{scheme=hybrid,epk,wrapped_dek,gw_key_fp}`; 密文 `ENC:v1:...`; env: `GATEWAY_CONFIG_DEC_ENABLED` | ❌（企业分支新增，属企业配置下发安全特性） |
| **EE-运行时编排** | Runtime Management |  | 替代直连 WS 的 AgentServerClient 扩展：经 Access/ServiceManager 编排多 AgentServer（K8s Pod 或本机进程），按企业策略解析 service/extension 配置后路由请求；配置变更防抖热重建 | AGENT_SERVER_DEPLOY_MODE=k8s\|process；防抖默认 2s；流式空闲超时 600s | env: `AGENT_SERVER_DEPLOY_MODE=k8s\|process`, `AGENT_SERVER_*`; section `service_config_templates` | ❌（企业分支新增，属分布式运行时编排特性） |
|  | 主备 Pod 治理 |  | 升主时清理旧主遗留 Pod 并重初始化 Access；采集 Pod 状态与业务量供 Manager 上报 | label jiuwenclaw-gateway-id 隔离 | K8s label: `jiuwenclaw-component=agentserver`, `jiuwenclaw-gateway-id` | ✅ 主备容灾：升主清理旧主遗留 Pod（先优雅停 Pod 再清理周边资源） |
|  | Gateway 优雅终止 |  | EE 扩展注册 SIGINT/SIGTERM 信号处理（K8s 删 Pod / Ctrl+C），置停机事件后主循环退出，按序停止频道、Cron、心跳、选主、Redis，并先优雅停 AgentServer Pod 再清理周边资源 | Linux 用 loop.add_signal_handler；Windows 兼容 SIGINT/SIGTERM/SIGBREAK；无扩展时保持原 gather 行为 | 信号 `SIGTERM`/`SIGINT` → `setup_gateway_shutdown_signals(shutdown_requested)`；退出执行 `client.disconnect()` | ✅ Gateway 优雅终止：SIGTERM/SIGINT 后干净退出，K8s 滚动升级/主备切换不残留 |
| **EE-元戎接入** | YuanRong 客户端 |  | gateway.agent_client.type=yuanrong 时改用元戎 Frontend HTTP/SSE 调 Serverless 函数（非 WS），以 X-Instance-Session: sessionID=service_id 保持实例亲和；兼容 file.transfer.* | 必填 frontend_endpoint、function_version_urn | config: `gateway.agent_client.type=yuanrong`, `frontend_endpoint`, `function_version_urn`; HTTP `POST /serverless/v1/functions/{urn}/invocations`（SSE 流式） | ❌（企业分支新增，属元戎 Serverless 接入特性） |
| **扩展机制** | Gateway 扩展加载 |  | 启动时经 ExtensionRegistry/ExtensionManager 加载全部扩展；支持替换 AgentServerClient、WebChannel 创建钩子、聊天前钩子等扩展点 | hook：WEB_CHANNEL_CREATED、BEFORE_CHAT_REQUEST 等 | hook 事件 `WEB_CHANNEL_CREATED`、`BEFORE_CHAT_REQUEST`; 扩展点可替换 AgentServerClient | ❌ |
|  | 可观测性 |  | 启动初始化 Telemetry（OpenTelemetry） | — | OpenTelemetry 标准 env（OTLP exporter） | ❌ |

## 说明

- **功能列合并**：「功能」列相同值的连续行仅首行填写（对应 Excel 合并单元格 rowspan）；「功能编号」列留空待填。
- **端口速查**：Web WS `19000/ws`、Gateway 多路 WS `19001`（`/acp`、`/tui`）、VibeSkill HTTP `19002` / WS `19003`、AgentServer 出站 `18092`、WhatsApp bridge `19600`。
- **三条控制链路**：Manager↔Gateway（EE，配置下发）、客户端↔Gateway（Web/ACP/TUI/IM/VibeSkill）、Gateway↔AgentServer（E2A over WS，或 EE 的 Runtime/元戎 HTTP）。
- **本地 RPC vs 转发 RPC**：Web 前端方法先查本地 handler（config/models/session/cron/heartbeat/channel/updater/memory/permissions 等约 70 个），命中转发集合的（chat/skills/skilldev/extensions 等约 45 个）经 E2A 透传 AgentServer。

## 「企业级分支额外添加」列说明

最后一列仅对**企业分支（dev/enterprise_kub，对照合流基线 545ed55f / v0.1.10）额外添加**、且属于以下三类特性之一的行打 ✅，并标注归属：

1. **主备容灾**：active-standby 部署模式、Redis 选主与单活联动、SessionMap/Cron/频道配置的 Redis 或共享 DB 存储、remote 会话、升主 Pod 清理等（共 10 项）。
2. **Web Server 文件下载**：Agent 产物经 `file.download.*` 分片下发 Gateway 落盘；配套 Web 下载链路——`send_file_to_user` 生成 HMAC 下载令牌（默认 600s 过期，密钥经 `JIUWENCLAW_FILE_DOWNLOAD_SECRET`/共享文件跨进程共享）、浏览器 `GET /file-api/download?token=`（流式 attachment，RFC 5987 中文文件名）、分布式下 Gateway `POST /file-api/push` 反向推 Web Pod 后换取新 token（`webserver/file_api.py`、`agentserver/tools/web_file_download.py`，均为本分支新增）。
3. **Gateway 优雅终止**：EE Runtime 扩展注册 SIGINT/SIGTERM（K8s 删 Pod / Ctrl+C），置停机事件后主循环退出并按序停止各服务，先优雅停 AgentServer Pod 再清理周边资源。

其余行标 ❌；企业分支虽有新增但不属于上述三类的（Manager 配置下发、加签验签、字段级解密、三级生效策略、Runtime 编排、元戎接入、link-auth、VibeSkill、多租户会话路由等），在 ❌ 后括号注明实际归属。
边界说明：上行 `file.transfer.*` 为分支新增但企业级部署（`AGENT_RUNTIME`）下被关闭，标 ❌；server push 旁路为主干能力，其 `file.download` 分支的 ✅ 归入「下行（Agent→Gateway）」行。

## 与设计文档的已知差异

- Cron 存储为 JSON 文件（standalone）/ Redis Hash（active-standby），非 SQLite；《Gateway-Cron-Redis存储设计》中的 `distributed`+Pub/Sub revision 方案，现码已演进为 `active-standby` + 按 `instance_id` 隔离的 Hash + 选主单活。
- 心跳服务在主备模式下未做单活门控，多实例会各自探活。
