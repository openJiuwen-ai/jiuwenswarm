---
id: B_20261126_0210_enterprise-team-file-live-delivery
title: 企业版团队会话文件卡片丢失：投递优先级反转与网关补发
type: B
author: hanxinyang
author_email: xyhan226@163.com
modules:
  - jiuwenswarm/agents/harness/common/tools
  - jiuwenswarm/enterprise/team
files:
  - jiuwenswarm/agents/harness/common/tools/send_file_to_user.py
  - tests/unit_tests/agentserver/test_team_session_file_delivery.py
designs: []
design_note: 针对 Issue #94 的投递链路局部修复，无独立设计。
---

# 企业版团队会话文件卡片丢失：投递优先级反转与网关补发

## 现象与影响

Issue [#94](https://gitcode.com/ZYQ5333/jiuwenclaw/issues/94)：企业版团队会话里 Agent 调用 `send_file_to_user` 后，工具返回"成功发送"，但 Web 端的下载卡片和右侧产物栏都看不到该文件。

定位基线为 upstream develop。非企业版会话、以及企业版非团队会话均表现正常，只有团队会话的 `chat.file` 帧丢失。

## 根因

企业版 Web 用户只订阅 `/sessions/{session_id}/live`，不建立会话级 WebSocket。通用 `send_runtime_push` 在 AgentServer 推送链路里返回成功——推送 hub 确实把帧交给了网关——但网关没有可转发的 web peer，帧在网关侧被丢弃。

因此"host push 返回成功"不等于"Web 端可见"。旧实现以 push 成功作为投递完成，团队的 `chat.file` 帧就停在了网关。

同时，`send_runtime_push` 还承担非 web 通道（`feishu`、团队人类席位等）的路由：`_build_push_message` 把 `target_channels` 写入 `metadata.send_file_targets`，由网关按此分发。直播（session live）只覆盖 web 订阅端，不能替代这条路由。

## 修复决策

1. 企业版团队会话优先调用 `publish_team_session_file`，让 Web 端和普通团队帧走同一条直播通道；非团队会话没有 live，仍走 host push，行为不变。
2. 直播成功后只在显式非 web 目标存在时补发 `send_runtime_push`，用 `send_file_targets` 判定。push 帧在无 web peer 时被网关丢弃，因此双发不会让 Web 端重复收到卡片。
3. 直播失败或抛异常时回退 host push，保留旧的兜底能力；直播成功而补发 push 抛异常时降级为 warning 并继续提交收尾。后者是本次新增的窗口：Web 端已经收到卡片，若整体报失败，`_mark_files_sent` 不会执行，Agent 重试会再发一张重复卡片。
4. 不改变文件暂存、下载 token、`_mark_files_sent` 去重和历史写入的既有语义。

## 回归范围

`tests/unit_tests/agentserver/test_team_session_file_delivery.py` 覆盖：

- 企业版团队会话在 push 失败时仍通过 live 送达；
- 直播成功时不再无谓调用 push；
- 显式非 web 目标（`feishu`）在直播成功后仍补发网关推送，并携带 `send_file_targets`；
- 没有 live 时回退 host push；
- live 抛异常时回退 host push；
- 直播成功但补发 push 抛异常时不报失败，仍执行 `_mark_files_sent` 并写入 `chat.file` 历史；
- 非企业版/非团队会话行为不变。

## 防错模式

投递"成功"要按目标端分别确认：浏览器订阅端看 live 订阅，非 web 通道看网关路由。任何只覆盖单一端的通道都不能作为整体投递的完成信号；补发失败也只应影响未被覆盖的目标，不能回滚已经可见的投递。
