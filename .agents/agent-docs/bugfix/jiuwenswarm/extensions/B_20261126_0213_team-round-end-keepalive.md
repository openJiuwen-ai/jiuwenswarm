---
id: B_20261126_0213_team-round-end-keepalive
title: 团队回合收口灯帧不停保活导致 Pod 永不回收
type: B
author: hanxinyang
author_email: xyhan226@163.com
modules:
  - jiuwenswarm/extensions/runtime_management_extension
files:
  - jiuwenswarm/extensions/runtime_management_extension/runtime_routed_client.py
  - tests/unit_tests/extensions/test_runtime_routed_keepalive.py
designs: []
design_note: 针对 Issue #144 的保活生命周期判定修复，无独立设计。
---

# 团队回合收口灯帧不停保活导致 Pod 永不回收

## 现象与影响

Issue [#144](https://gitcode.com/ZYQ5333/jiuwenclaw/issues/144)：企业版团队会话结束后 Pod 永远进不了 idle 池，无法回收。

链路是：回合收口灯帧到达 → deferred keepalive 仍在每 20s touch 会话 → 会话 TTL（60s）永不到期 → runtime 不 evict → Pod 的会话集合始终非空 → 不通知 idle → `reclaim_once` 只遍历 idle 池 → Pod 永远回收不掉。

## 根因

`_stop_deferred_keepalive_if_round_ended` 看到企业版灯帧带 `idle=True` 就直接返回，把它理解成"团队还在这台 Pod 上，只是本轮灯灭了，继续 touch"。

这个理解是错的。生产方 `team_helpers._quiet_round_status_payload` 的 docstring 明确 `idle` 只是"Enterprise keeps the SSE open"的前端连接保持标志，与后台是否还在跑无关。消费方把前端的连接标志当成了运行时忙闲状态。

## 修复决策

1. 删除 `if payload.get("idle") is True: return`，改为以帧本身的收口语义判断：`chat.processing_status` 且 `is_processing is False` 即停止该会话的 deferred keepalive。
2. 不用 `is_complete` 进一步收紧：生产方所有 `is_processing=False` 的 `chat.processing_status` 帧都满足 `is_complete=True`（灯帧、`_team_processing_done_chunk`、`team_create_failed`、pause 帧），`is_complete` 不提供额外信息，反而会在字段缺失时漏停。
3. 被 `hold_idle` 按住的 `team.idle`（swarmflow 仍在跑）不会产出这帧，所以"按帧收口"不会误停还在跑的回合。
4. 停止后不受影响：下一次 `chat.processing_status_deferred` 会重新拉起 keepalive；流内保活与 deferred 保活仍是两条独立路径。

## 回归范围

`tests/unit_tests/extensions/test_runtime_routed_keepalive.py`：

- 带 `idle=True` 的收口灯帧停止保活，`touch_calls` 不再增长；
- 收口后再次收到 `chat.processing_status_deferred` 能重新拉起保活；
- 收口帧仍会转发给上层推送处理，`_session_keepalives` 清空；
- 流式连接未结束时的既有 touch/重路由行为不变。

## 防错模式

消费方对字段语义的判断必须回到生产方的定义。一个同时承载"前端连接保持"和"运行时忙闲"两类含义的字段，不能只用名字推断；语义反转时要把生产侧证据写进文档，避免后续维护者按旧理解加回保护。
