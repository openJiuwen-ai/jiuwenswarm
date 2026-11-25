---
id: S_02_colleague-swarmchat
title: SwarmChat AI 同事接入契约
type: S
author: weichenhao
author_email: weichenhao3@huawei.com
modules: [jiuwenswarm/server/runtime, jiuwenswarm/server/jiuwenmate, jiuwenswarm/agents/swarm, jiuwenswarm/channels/web/frontend]
files: [jiuwenswarm/server/jiuwenmate/swarmchat/bridge.py, jiuwenswarm/server/jiuwenmate/swarmchat/port.py]
---

# SwarmChat AI 同事接入契约

## 身份与生命周期

`group.create` / `group.member.add` 接受 `kind=colleague`、`id=agent_id`。用户必须拥有目标群，同事必须存在且启用；同一同事不可重复加入同一群。
`group.json` 的 members 是成员关系权威；compiled 保存 core 身份、头像和成员生效时间。
core 使用 HOST_MANAGED，登记与恢复不创建群内智能体。执行仍使用同事原有调度、权限与配置。
群运行时构建只使用已保存的成员身份，不检查同事是否可用；同事停用或删除不影响群内其他成员。可用性只在新同事入群（不可用返回 BAD_REQUEST）和每次执行前检查。
建群令牌和加人回执按成员身份比对。同事名称与简介由宿主解析，展示资料变动不影响相同请求的重试。
SwarmChat 公开群运行时（conversation_mode=group_chat）的成员登记不受普通团队人数配额限制，覆盖专家、专家团身份和 AI 同事。
群运行时不挂载团队人数 guard、不作为企业团队占用登记或按群名册对账配额，不对首条群消息执行建团人数预检；如有旧 guard 则撤销。
普通团队和专家团内部执行仍保留配额检查、租约和并发保护。宿主管理身份在其他团队中亦不占运行名额。

群 Session 使用 `(namespace_id, agent_id, swarm_group, swarmchat, group_id)` 唯一定位，复用 agents.sqlite3 的 sessions 表。
不保存 history.jsonl 绝对路径、不写入同事 IM 群表。当前 SwarmChat 接入使用 local namespace。

## 消息与恢复

只处理已经持久化、结构化 mentions 指向同事的公开消息；用户、专家、同事的消息均适用。入群前历史可查，但旧 @ 不重放。
回调提供即时通知，只把处理交给后台任务，不阻塞 core Leader 的消息循环；常驻接入任务约每 5 秒补扫。
补扫按 swarmchat_scan 中每个（群, 同事成员）的位置只处理之后的消息，以只读方式读取 core 消息表，不重写 history.jsonl；某条提及处理失败或仍在由回调处理时，该成员的位置停在这条之前。单个群或成员的异常不影响其他群。
swarmchat_mentions 持久记录处理状态，原同事请求的 source_ref 再次去重。
自动接力以最近用户消息为范围，每群最多接受 5 次自动触发；计数与预留在 SQLite 事务中完成。
停止群会取消相关同事请求并阻止该范围继续触发，新用户消息建立新范围。同事侧停止只对含同事的群执行，失败只记录日志，不影响原有专家与组织停止。同事停用、删除或成员失效后停止新执行与未完成工作。

回复通过 ResultDeliveryService 发件箱交给 core append_group_message，以原成员署名写入群历史。
固定 client_message_id 保证投递幂等；群运行时不在池中时保持待投递，下一次就绪后重试。
重启重新认领中断的 SwarmChat 发送并复用同一消息键。此前已标记为 unknown 的发送不会因重启自动重投。
回复使用结构化 mentions 唤醒其他成员，宿主处理同事，core 处理专家。执行过程不自动公开。

## 群成员读取

SwarmChat 成员信息来自 group.json 名册，飞书成员信息来自飞书成员接口。两者均不根据发言者推断成员。
- 每轮执行经系统提示附加段注入当前群名册（不写入会话历史）：每人显示名、类型（专家 / 专家团 / AI 同事）、是否本人、`[[agent:<member_name>]]` 提及标识与截断到 80 字的简介，另注明群名、群 ID、成员数与人类群主；超过 30 人只列前 30 人并提示调用工具。
- `list_group_members(platform?, group_id?, group_name?)` 为宿主工具，仅注入 local namespace 且至少在一个 SwarmChat 群中的同事。省略参数返回当前群；群外会话必须指定目标。
  授权与 SwarmChat 跨群消息相同：同事当前为成员，当前为 SwarmChat 群会话时群主须一致；每次调用及返回前复核。不在范围或不存在统一返回 unavailable，不暴露群是否存在；同名群要求改用 group_id。
  成功返回完整名册（完整简介、提及标识、是否本人）、`complete=true` 与 `member_count`。
- 飞书查询显式指定 `platform=feishu`，目标使用已授权群的 ID 或名称。仅有一个获授权飞书群时可以省略目标。宿主复用同事 IM 群访问校验，使用匹配绑定的飞书应用读完整分页，返回前复核授权。返回成员显示名，分页或访问失败时不返回部分名单。
- 飞书名单查询不经过 Shell 或分享审批；钉钉和 WeLink 名单当前不支持。
- SwarmChat 的宿主预检查把已识别的飞书查成员 CLI 和按成员读取 scope 发起的 CLI 登录导回只读工具。查询失败时说明限制。

## 历史、跨群与文件

自动背景复用 core CONTEXT_TAIL：当前请求消息加最多 4 条前序未读消息；触发内容不重复注入，前序正文最多 2,000 字符。
search_group_messages 统一搜索 IM 与 SwarmChat；同事的启用连接按配置身份关联。SwarmChat 每次验证当前成员关系，当前群会话按群主收窄范围。
缺少 IM 历史配置时，SwarmChat 群会话仍可读本群和其他授权 SwarmChat 群。IM 当前身份在该群已有授权时优先使用它；只能回退到关联身份时，使用已核验关系中最晚的可见下界。
结果保留平台、群、消息和时间，支持过滤及分页。组合分页固定时间上界与结果指纹，历史或权限变化时拒绝旧页。
消息查询在有效授权内免跨群分享审批；文件与在线文档的原有授权继续生效。目录只提供元数据，不能代替读取授权。

群文件从公开附件、公开产物和 file_overrides 解析。宿主不扩大通用路径权限，而是通过现有 Session output/imports 边界导入副本。
导入登记在 swarmchat_file_imports；后续工具读取复核成员资格、来源及字节摘要，失效时撤销私有副本。原文件不因同事修改副本而改变。
清空历史过滤后续消息查询，不能追溯抹除已经进入模型上下文的内容。

## 审批与兼容

需要确认的工具操作投影到现有群提问卡片，绑定原审批、同事、Session 与具体 tool_call_id，由群所属用户决定。审批项展示脱敏、限长的调用摘要，批准只适用于所选调用。
批准、拒绝、过期及一次性消费继续由 PermissionApprovalRepository 处理；消息免分享审批不绕过工具操作审批。
接收审批决定后，群会话向当前订阅者发布 chat.ask_user_answer 事件，收起对应卡片。
同事参与聊天提及，不进入专家任务卡片执行接口。旧群和原 IM 接入保留原执行流程。
本契约依赖 core 的 HOST_MANAGED 实现；尚未包含该能力的固定依赖须先升级。

## 自动判断回复

适用于 SwarmChat 的专家、专家团代理和 AI 同事；群内只有群主是人类。
- 触发：仅已接受的、`mentions` 为空的用户消息。带 @ 的消息与成员发出的消息不判断，沿用原有链路。
- 群配置在 `group.json` 顶层：`auto_reply = {enabled, model_selection}`，缺省为开启且不指定模型，经 `group.update` 修改，群公开信息原样返回。开关对全群生效，没有按成员的回复规则；旧数据中残留的 `reply_rules` 被忽略。
- 判断模型取 `auto_reply.model_selection`，为空时取群会话的模型选择。每次判断是一次无状态调用，提供基础工具 `notify_member(member_name, reason)`；群内至少有两个专家团代理时另提供下文的 `coordinate_expert_groups`。独立通知的调用次数由模型决定，宿主忽略非法、非群成员及重复调用。超时 30 秒。
- 判断不修改消息：core 中该消息的 `mentions` 与 `history.jsonl` 保持用户发出时的内容。
- 判断结果保存在群会话目录的 `auto_replies.json`，键为 core 的 `message_id`，状态为 `pending` / `notified` / `none` / `failed` / `stopped`，`notified` 逐成员记录是否已送达或已丢弃。群历史响应以 `auto_replies` 返回未被清空的记录，变化时发布 `group.auto_reply.updated`；网关只转发事件标识，前端收到后从群历史响应读取结果。同一消息只判断一次。
- 送达：AI 同事经 `ColleagueGroupBridge.notify` 进入同事调度器，与 @ 共用 `swarmchat_mentions` 预留、请求 `source_ref` 和停止处理；专家与专家团代理经 core `TeamBackend.notify_group_member`，通知标识为 `auto-reply:<message_id>:<member_name>`。送达前按当前名册与群开关复核，不符合或被 core 拒绝的通知标为已丢弃，不再重试。
- 被 `notify_member` 通知的专家与专家团代理的群轮次固定为 `direct`，这类通知不会形成 Team Organization。组织只来自用户显式 @ 多个专家团，或下一条的协作决策。
- 协作决策：群内至少有两个专家团代理时，判断模型另有工具 `coordinate_expert_groups(owner, members, reason)`，表示这些专家团要共同交付一份成果。宿主只接受成员去重后至少两个、全部是当前专家团代理且包含 `owner` 的调用，其余忽略；一条消息只取第一个合法的协作决策，并丢弃同一次判断里的 `notify_member`。记录增加 `organization = {owner, members}`（Owner 在首位），`notified` 只含 Owner，群历史响应原样返回该字段。
- 协作决策的送达：先以用户消息的 `client_message_id` 和持久化正文调用 `group_route.record_candidate` 登记组织候选，再只通知 Owner。之后与显式多 @ 相同：Owner 选择普通回复、追问或 `run_team`，接受任务后由现有组织流程拉其余专家团加入并汇总回复。送达前成员已离群或不再是专家团代理、或候选登记被拒时，该决策标为已丢弃，不唤醒任何成员，也不降级为独立通知。
- 协作决策下的群轮次：判断记录与组织候选都包含该成员时轮次为 `organization`，Owner 与其余专家团相同；只有判断记录而候选未登记时不形成组织。
- 恢复：运行时就绪、周期扫描以及同一群的下一次判断会补投未送达的通知。周期扫描覆盖没有 AI 同事的群；中断的判断在 10 分钟内重新判断，之后记为失败。判断失败不自动重试。
- 停止：群聊页面不提供整群停止按钮。经其他入口发起的整群取消会取消进行中的判断并记为 `stopped`（不再重判）、把尚未送出的通知标为已丢弃；已交给 core 或同事调度器的通知不撤回，成员仍可能回复。失败只记录日志，不阻断原有取消。单个专家、组织、定向请求的停止和客户端断连触发的清理不影响全群判断。整群停止后，尚在恢复流程中运行的判断结果会被丢弃。
- 消息写入后、判断调度前进程退出时，该消息不会被判断。
