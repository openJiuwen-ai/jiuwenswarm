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
