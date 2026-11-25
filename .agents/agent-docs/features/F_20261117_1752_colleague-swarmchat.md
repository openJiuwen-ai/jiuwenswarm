---
id: F_20261117_1752_colleague-swarmchat
title: AI 同事接入 SwarmChat 实施记录
type: F
author: weichenhao
author_email: weichenhao3@huawei.com
modules: [jiuwenswarm/enterprise/team, jiuwenswarm/server/runtime, jiuwenswarm/server/jiuwenmate, jiuwenswarm/agents/swarm, jiuwenswarm/channels/web/frontend]
files: [jiuwenswarm/server/jiuwenmate/swarmchat/bridge.py, jiuwenswarm/server/jiuwenmate/swarmchat/port.py, jiuwenswarm/server/jiuwenmate/swarmchat/delivery.py, jiuwenswarm/server/jiuwenmate/swarmchat/roster_tool.py, jiuwenswarm/channels/web/frontend/src/features/groupChat/MemberPicker.tsx]
designs: [D-20261116-1201-colleague-swarmchat-membership]
---

# AI 同事接入 SwarmChat 实施记录

## 实际范围与决定

依据 D-20261116-1201-colleague-swarmchat-membership 实现建群与动态加同事、独立 core 身份、持久 @ 请求、同事调度、回帖和审批卡片。
SwarmChat 与 IM 共用 search_group_messages，消息免分享审批，读取仍检查身份和成员资格；IM 连接按同事配置归并。
本次消息审批策略替代原消息查询必须批准的规则；文件、在线文档及工具操作审批保留。已迁移相应旧测试，保留底层授权和撤销覆盖。

不重新实现智能体运行时、执行调度或发件箱。core 回调不代表请求已经接受，由 SQLite 处理记录和原请求幂等键保证恢复。
采用常驻补扫兜住暂停与重启期间的通知缺口；成员关系仍在 group.json，不创建第二份授权名册。

文件实现采用现有 Session 导入接口，未放宽 core 路径校验；与设计中的本群直接只读集合有所不同，宿主返回副本，原文件保持独立。
导入来源与摘要在宿主数据库登记；读取时重新核验，不依赖模型可写的 manifest。

## 验证证据

- 最后一轮相关全量回归：2109 通过、1 跳过、6 失败。失败中的 4 项为新审批策略与 local namespace 隔离对应的旧断言，更新后相关 89 项全部通过；另 2 项子进程清理用例受沙箱限制，此前提升测试进程查询权限后均通过。
- 最后定向回归 89 项通过，包含目录跨连接归并、禁用身份隔离、SwarmChat 与指定 IM 联合查询的平台过滤，以及最近背景、跨群分页、原子预算与文件撤销。未在最后修改后重新运行整套回归。
- 使用真实 core SQLite 与公开群消息接口验证 @ 补扫、重建接入不重跑、成员署名和重复投递幂等；智能体执行不使用真实模型。
- core 既有群聊／人类协作回归 117 通过；core 独立新增角色测试 2 项通过，swarm 真实数据库用例另覆盖回调与投递。
- 前端 group-chat 状态测试 41 通过，npm run build 通过。未运行格式化或静态检查工具。

## 环境与交付边界

core 源码改动位于相邻 agent-core 仓库。当前 .venv 使用该本地仓库的 editable 安装；本仓远端 core 固定提交尚未变更。
正式部署前需要发布 core 改动并更新依赖固定版本；重新 uv sync 可能恢复旧 core。
前端补齐了锁文件已有的 oidc-client-ts 及 jwt-decode，仅调整 node_modules，未新增依赖或修改锁文件。

尚未进行真实模型、飞书／钉钉／WeLink 与 Web 群的端到端联调，也未取得实际页面截图。
补扫、跨群合并使用有界扫描，群与历史很多时需要真实负载验证；结果不完整时不能推断没有更多历史。
同事的运行中过程和任务卡片仍不公开；没有实现跨群私有会话记忆的自动共享。
本次不提交、不推送，保留用户其他改动。当前契约见 S_02_colleague-swarmchat；底层角色契约留在 core。

## 本地依赖回退故障与恢复（2026-11-17）

实际 Web 添加同事时报 group request failed，AgentServer 日志为 AttributeError: HOST_MANAGED。
调试启动通过 uv run / uv sync 恢复了固定提交的旧 core，本地 editable 安装不足以保证重启后的源码版本。
恢复时先安装本地 core，再在当前本地配置与 domain 配置的 PYTHONPATH 前置同级 agent-core 与本仓源码。
此配置仅用于该开发机，不提交真实 .env，不修改生产依赖固定版本；正式交付仍需发布 core 并更新固定版本。
启动保持原 JIUWENSWARM_DATA_DIR 指向 domain；曾遗漏该变量导致 group not found，按原目录重启后恢复，无群数据删除。

验证从原 Web 页面重试已选中的小九：弹窗关闭，D2 群人数从 6 增至 7，右侧显示小九 AI 同事。
独立读取 group.json 确认 colleague 身份、agent_id 与 compiled 的 host_managed 角色持久化。
相关同事接入与群装配回归 39 项通过，git diff --check 通过。未发送群消息，未触发模型或外部 IM。

## 真实提及链路修复（2026-11-17）

实际 @ 报 MemberRosterEntry 没有 role：core 冷恢复把轻量名册投影当完整成员。
core 改为通过公开 TeamBackend.get_member 获取持久化角色，缺失成员跳过，宿主管理身份不启动内部运行时。
恢复后同事执行 completed，但回复在发件箱领取阶段被标为 ROUTE_MISSING：该校验仍把 IM app_id 强制用于 SwarmChat。
宿主修正为只有 IM 路由要求 app_id，本地 SwarmChat 仍校验路由存在和渠道一致；其他授权检查保持。
新增测试从真实请求、候选输出、结束事件、发件箱领取到发送回执，验证不带 app_id 的 SwarmChat 正常投递且不会再次发送。

验证：core 恢复与角色 7 项、既有群聊 117 项通过；宿主接入／提交／发件箱相关 102 项通过。
原 domain 实例已重启。此前因 ROUTE_MISSING 失败且 attempt_count=0 的单条回复恢复为 pending，沿用幂等键投递，不重跑该轮模型。
Web 页面实测小九以自身身份回复问候，持久化发件箱显示 delivered；另发同样最小问候验证重新激活链路。
此证据覆盖真实模型的问候链路，不代表跨 IM、文件与审批的端到端验收。

## 身份名额误计修复（2026-11-18）

动态添加同事出现 TEAM_MEMBER_QUOTA_EXCEEDED：宿主对 spawn_member 的配额包装没有区分 host_managed。
登记时该角色跳过普通队员配额；公开 quota_member_names 从真实持久化成员角色筛选计费名册，供人数统计和冷恢复租约对账共用。
模板预检也排除宿主管理身份；没有放宽普通专家限制，未修改 core、人类成员规则或用户套餐。
重启对账沿用已有 reconcile_local_roster 的释放逻辑，清理历史误计身份租约。

相关 64 项测试通过，包含租约与名册两条入口、已有同事不挤占专家容量、满额后普通专家仍被拦截，以及真实 core SQLite 上满额登记多个同事、重复身份仍被拒绝。
服务按原 domain 目录重启，原页面重试系统架构工程师入群后弹窗关闭、配额错误消失，右侧列出三位 AI 同事。
实际页面验证未发送新群消息或触发模型；真实 core API 的登记边界由上述数据库测试覆盖。
中英文使用说明和当前接入契约同步，git diff --check 通过；未提交或推送。

## 群成员名册与团队配额分离（2026-11-18）

按用户确认，取消将普通团队人数配额用于 SwarmChat 公开群名册的行为，范围不限于 AI 同事。
成员 guard 依据可信 backend.conversation_mode 识别 group_chat，拒绝挂载并撤销已挂载的旧 guard。
运行时就绪接线对公开群提前返回，不登记企业团队占用、不按群名册回填租约；首条群消息不做普通建团人数语句预检。
普通团队模式及专家团内部执行仍保留原配额；未把默认值改为 100，也未改变任何用户策略。

验证：名额 guard、team_helpers 和 GroupService 回归 349 项通过；配额策略、建团协议、群请求和同事接入回归 68 项通过。
包含真实 core SQLite 群名册登记超过普通团队上限、旧 guard 移除、就绪时不调用配额 actor、普通团队超员仍被拦截，以及既有群成员 101 后继续加人场景。
原 domain 实例已重启，四个服务端口就绪；本次没有往实际群新增无关专家或发送模型请求。
中英文使用说明和当前契约同步；git diff --check 通过，未提交、未推送。

## 检视问题修复（2026-11-18）

修复本次检视中的 P1-2、P1-3、P2-1、P2-2；P1-1（发布 core 并更新锁定版本）与 P2-3（拆分夹带的配额与 IM 审批策略变更）不属于代码修复，仍待处理。

- P1-2 同事停用或删除导致整个群无法构建：`assemble_group_members` 改为只用 group.json 保存的身份构造 HOST_MANAGED 成员，不再查询同事服务；
  `GroupService._compile` 对已入群同事沿用 compiled 中的身份，只对新同事检查存在与启用，不可用时返回 BAD_REQUEST。执行前的可用性检查与不可用提示保持不变。
- P1-3 补扫与回调每秒全量读写历史：补扫间隔改为 5 秒，按新表 swarmchat_scan 中每个（群, 成员）的位置增量处理；
  `GroupService.public_messages` 改为只读查询 core 消息表，不再经过会重写 history.jsonl 的 sync_history（群聊记录 RPC 仍维持原投影行为）；
  core 回调只创建后台任务即返回；同一提及由进程内在途集合去重，补扫遇到在途或失败的提及时停住位置，不越过未确认消息。
- P2-1 单个群异常中断全部群：补扫按群、按成员分别捕获异常并记录。
- P2-2 群停止依赖 JiuwenMate：只在群内有同事时调用同事侧停止，异常记录后继续原有专家与组织停止。

why-not：未改用 core 的成员广播已读水位，因为同毫秒消息会被 `timestamp > read_at` 跳过；
补扫仍按群读取全部公开消息（只读），未新增按时间过滤的 core 查询接口，大群负载仍需真实验证。

验证（本地 agent-core editable，PYTHONPATH 前置；未使用锁定 core 版本）：
- 新增 `tests/unit_tests/server/jiuwenmate/test_swarmchat_recovery.py` 6 项：已保存同事构建不查询可用性、已入群同事沿用身份且新的停用/删除同事被拒、只读历史不重写投影并过滤清空时间点、损坏群不影响其他群且已处理提及不重复提交、回调立即返回且补扫等待在途处理、同事停止失败时群停止继续及无同事群不调用。
- 同事接入、免审批搜索、群提问、配额、共享与缓存撤销、调度器相关 211 项通过；群服务、群执行、群请求、群续做、组织路由与停止控制、JiuwenMate 回归 376 项通过。
- 未运行前端测试、端到端联调或负载测试；未重启实际服务验证。

重启实测（2026-11-18）：用 `.venv/bin/jiuwenswarm-stop` / `jiuwenswarm-start debug --skip-build` 重启（未用 `uv run`，避免同步回锁定 core），
`JIUWENSWARM_DATA_DIR` 保持 domain，AgentServer 进程确认 PYTHONPATH 前置本地 agent-core。
- 页面新建群“检视验证”并选入小九，@ 小九后约 30 秒以小九身份回复“收到”；请求 completed，发件箱 delivered 且只投递一次；swarmchat_mentions 记为已接受，swarmchat_scan 推进到该消息。
- 关闭群页面后 30 秒（约 6 轮补扫）该群 history.jsonl 未被改写；打开群页面时仍会周期改写，来源是前端每 10 秒拉取群聊记录的既有行为，不属于本次补扫。
- 重启后日志中无补扫失败、提及未接受或同事停止失败记录。
- 未实测：停用/删除同事后的群冷恢复（避免修改用户已有同事，由单测覆盖）、群停止按钮、自动接力上限、跨群搜索、文件与审批。

## 群成员感知（2026-11-18）

问题：用户在另一个群问“D8 群里有哪些人”，同事只能用 search_group_messages 读消息，按发言者推断出 3 人，而 D8 名册有 12 人。

决定（用户确认，先做 SwarmChat）：本群成员每轮注入，其他群与完整信息用工具查询；两者都直接读 group.json，不另建成员表。
- 每轮注入：`swarmchat/files.py` 的 `execution_guidance(group, agent_id)` 经系统提示附加段注入本群名册，不写入会话历史，因此每轮最新、不累积。
  每人一行：显示名、类型（expert / expert team / AI colleague）、是否本人、`[[agent:...]]` 提及标识、截断到 80 字的简介；另注明群名、群 ID、成员数及人类群主。
  超过 30 人只列前 30 人并提示调用工具；明确要求成员只以名册或工具结果为准，不得按发言者推断。
- 工具：新增 `swarmchat/roster_tool.py` 的 `list_group_members(group_id / group_name)`，按 `agent_mail_tool` 的宿主工具方式在执行时注入，不改 agent-core。
  仅对 local namespace 且至少在一个 SwarmChat 群中的同事注入；省略参数时返回当前群，群外会话必须指定目标。
  授权沿用 SwarmChat 跨群消息规则：同事当前是成员、群主一致（当前为 SwarmChat 群会话时），每次调用复核，返回前再次确认成员关系；不在范围或名称不存在统一返回 unavailable，不暴露群是否存在；同名群要求改用 group_id。
  返回完整名册（含完整简介）、complete=true 与 member_count。名册公共辅助函数为 `membership.roster`。
- why-not：未采用入群时一次性注入加成员变化推送，因为同事按请求执行，变更时无运行进程可通知，写入历史还会留下过期名单；未采用仅工具方案，因为 @ 依赖随机 member_name，未查询时提及会被丢弃，且每次多一轮模型调用、工具结果还会留在历史中。
  未把所有群名册放入提示词，以免群多时撑大上下文。IM 群成员需平台接口或事件缓存，另期处理。
- 工具名不在 core 的只读工具白名单内，会按保守写操作记录意图；权限策略与 search_group_messages 相同，默认放行、自动档位走自动审批。

验证（本地 agent-core editable）：
- 新增 `tests/unit_tests/server/jiuwenmate/test_swarmchat_roster.py` 8 项：名册注入包含类型/提及/本人/未发言成员及约束、大群截断提示、工具读取当前群与他群并含完整简介、非成员/他人群主/不存在的群返回 unavailable 且无名册、群外会话需指定目标与同名群歧义、仅符合条件时注入工具并同步 fingerprint、重复声明拒绝。
- 相关同事、群服务与群请求回归 213 项通过。

工作区事件：本次开发中发现本记录与 `test_swarmchat_recovery.py` 已从磁盘消失（reflog 显示 15:14 切换至 releases/0.2.9 并 rebase，16:35 切回 develop；两文件未曾提交，git 中不可恢复）。
已按此前写入的内容原样恢复两文件，并追加本节。

重启实测（2026-11-18 17:29 启动，domain 数据目录，本地 agent-core）：预览浏览器身份下新建“名册验证”群（学术论文写作专家 + 小九，专家从未发言），
在“检视验证”群 @ 小九询问“名册验证”群成员。执行事件显示小九调用 `list_group_members(group_name=名册验证)`，结果 found、complete=true；
回复列出 2 位成员及类型（专家 / AI 同事，并标注本人），说明群主为人类用户；请求 completed，发件箱 delivered 一次。
未实测：在当前群直接询问本群成员（仅注入路径，由单测覆盖）、超过 30 人的大群、他人群主的群拒绝（由单测覆盖）。


## 飞书名册与群审批摘要（2026-11-19）

从 SwarmChat 查询飞书群成员时，同事先前需多次调用 CLI，每个 Shell 调用分别触发审批。现有 list_group_members 同时支持飞书，通过已有群访问校验取得绑定应用，直接读取飞书成员接口，完整分页与再次校验通过后返回显示名。
群审批卡附上脱敏的具体工具摘要，沿用逐次批准的语义。群聊接收 colleague_permission 实时事件，并继续按请求 ID 去重。

后端定向测试 48 项通过，覆盖分页失败、访问失效与审批摘要脱敏。前端群聊测试 41 项通过。飞书真实租户权限需以实际查询结果为准。


### 旧 CLI 路径回退修复（2026-11-19）

真实请求中只读工具已装配，但模型沿用历史调用，通过 Shell 查成员后又尝试登录和出示授权二维码。候选二维码未通过当前 Session 文件校验，最终回执失败。
SwarmChat 提示要求先用名单工具并在不可用时停止。群工具预检拦截已识别的旧查名单及对应的登录命令。员工自动审批模式在权限判定前处理同一限制。
定向回归 53 项通过，覆盖原始命令、名单读取 scope 登录和其他出入口；真实飞书名单未重跑。


## PR #10644 检视修复

成员请求按身份去重，展示资料单独编译。群历史端口在无 IM 配置时独立运行，IM 关联身份搜索保留被选中身份的可见下界。
审批答复通过群事件同步，元数据异常不阻断普通成员停止。SwarmChat 发件箱仅恢复中断的 sending 行并保留已有 unknown。
回归覆盖建群和加人重试、独立历史端口、关联身份读边界、审批事件、停止隔离、发件箱恢复及附件分享的投递范围和撤销。

## 单测报告回归修复

SwarmChat 名单工具装配遇到缺失的同事配置时跳过可选工具，非 SwarmChat 工具预检继续由 IM 端口处理。
固定官方 core 提交 54886e9bb，提供 HOST_MANAGED 并保留宿主仍使用的 Symphony DiscoverySession 接口。
rebase 到上游 5600f8673（宿主已迁移到统一 Symphony 检索并不再使用 DiscoverySession，core 固定为 fdc7cd720）后，固定版本改为上游 develop 上的 55756c559：它在 fdc7cd720 之上合入 HOST_MANAGED，core 依赖声明未变。验证（本地 core 即 55756c559）：SwarmChat、群服务、配额、Symphony 相关 1119 项与权限相关 2086 项通过。
内存热重载用例改用实际 DeepAgentParts 数据结构，覆盖 extension_bus 字段。
