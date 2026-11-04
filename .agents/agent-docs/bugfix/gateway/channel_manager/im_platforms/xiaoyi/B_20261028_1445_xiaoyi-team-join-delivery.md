---
author: zhongxiaotian
author_email: zhongxiaotian@huawei.com
id: B_20261028_1445_xiaoyi-team-join-delivery
title: 小艺成员加入团队后仅在通知栏收到 Team Leader 消息
type: B
modules:
  - jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi
  - jiuwenswarm/gateway/message_handler
files:
  - jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi/xiaoyi_connect.py
  - jiuwenswarm/gateway/message_handler/join_exit_handlers.py
  - tests/unit_tests/gateway/test_xiaoyi_team_delivery.py
designs: []
design_note: 当前团队席位和 fan-out 契约未变化，本次仅修复小艺平台任务生命周期。
---

# 小艺成员加入团队后仅在通知栏收到 Team Leader 消息

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-28（Asia/Singapore） |
| Issue | [GitCode #5744](https://gitcode.com/openJiuwen/jiuwenswarm/issues/5744) / [GitHub #9408](https://github.com/openJiuwen-ai/jiuwenswarm/issues/9408) |
| 严重程度 | 中；小艺人类席位无法在当前团队轮次中收到聊天消息 |
| 范围 | 小艺 Channel 任务生命周期、Gateway `/join` 成功回执 |
| 测试基线 | 相关 36 项通过 |

## 现象

小艺用户执行 `/join` 成为 Team 人类席位后，Team Leader 发送的
`team.message.p2p` 只出现在手机系统通知栏，没有进入小艺聊天窗口。
成员路由、`fan_out_targets` 和 Team Leader 消息生成本身均正常。

## 根因

`/join` 成功回执是普通 `chat.final`。该回执没有 `routing_target`，因此进入
小艺 `_send_legacy()`；后者以 `is_final=True` 发送并清理
`_active_push_sessions`。随后 Team Leader 的异步成员消息进入 `_send_team()`，
此时没有可复用的平台 `taskId`，只能降级到 push webhook。

## 决策

- Gateway 仅为小艺的成功 `/join` 确认和加入前历史消息增加内部 metadata
  `xiaoyi_team_task_hold=True`；失败、重复加入、飞书和 `/exit` 不设置该标记。
- 小艺将带标记的回执发送为 `lastChunk=True, is_final=False`，并在内存中记录
  `(physical_session_id, task_id)` 持有项，保留活跃 WebSocket 路由与 keepalive。
- 持有任务中的普通 Team `chat.final` 只展示正文，不提前结束平台任务。连续消息
  不按短空闲窗口关闭；收到 Team 轮次终态或 `chat.error` 时统一收口。
- Member 请求产生的 `chat.processing_status_deferred` 也不是平台生命周期终点。
  带 `member_name` 的无类型状态帧必须保留当前 task route，否则后续成员正文会错误
  降级到 push。
- Team 轮次终态本身没有用户可见正文。持有任务的成员在此统一收口；没有 hold
  的成员直接忽略该控制帧，避免向仍然在线的连接发送空文本 final。
- 持有任务收到 `chat.error` 时，错误正文以非终态发送，随后由统一收口路径发送
  唯一一次 failed 状态 final；避免同一任务连续收到两个 final。
- 新入站任务会清除旧回执的持有状态；普通单 Agent final、`/exit`、超过活跃
  窗口的无路由消息以及现有 push 截断行为保持不变。

## 未采用的方案

- 仅删除 final 后的 `_active_push_sessions` 清理会让普通任务继续被保活，
  且仍可能向已结束的旧 `taskId` 发送消息。
- 在小艺 Channel 内解析 `/join` 文本会把渠道适配与 slash 命令语义耦合；
  Gateway 已经掌握“成功加入”这一事实，因此只传递内部标记。
- 所有 Team 消息无条件切换为 push 无法满足在线成员在聊天窗口中接收消息的契约。

## 验证

- `tests/unit_tests/gateway/test_xiaoyi_team_delivery.py`：12 项通过，覆盖成功
  `/join` 持有、Team p2p 走 WebSocket、Team final 延后收口、轮次终态、错误、
  deferred 状态保留路由、`/exit`、普通 final、无 hold 空终态防护和离线 push 兜底。
- 连同小艺 session 复用、push 截断和 `/join` 校验，本组共 36 项通过。
- `test_team_helpers.py` 与终态 fan-out 用例合计 245 项通过。
- 网关全量单测：1481 项通过、6 项跳过、3 项失败。失败均位于未修改的
  Windows 路径用例（YAML 盘符转义、HarmonyOS 路径分隔符、上传文件名反斜杠），
  与本次小艺/Team 改动无关。
- 未连接真实小艺客户端验证聊天窗口表现；仍需在含人类席位的真实团队中完成
  `/join`、Team Leader p2p、轮次结束和离屏通知的手工验收。

## 已知遗留

小艺 WebSocket 不提供消息 ACK。成员在团队轮次中途异常断连时，网关仍沿用现有
活跃窗口和 keepalive 启发式判断，不能保证检测到端侧已关闭聊天窗口。
