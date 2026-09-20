---
id: CHG-20260908-002
title: Route every user channel through the single-Agent Session Runtime
type: refactor
date: 2026-09-08
modules:
  - runtime-session
  - agentserver-runtime
  - gateway
flows:
  - runtime-session-reference-chain
  - agentserver-session-lifecycle
confidence: confirmed
---

# Route Every User Channel Through The Single-Agent Session Runtime

## What Changed

`AgentRuntime` now natively routes foreground Work Normal and Code Normal requests through `RuntimeSessionCoordinator` for registration, scheduling, indexing, cancellation, and close. Plan, Team, and background requests continue through their distinct executors.

Runtime registers eligible sessions when create/switch commits and lazily adopts direct callers at its execution boundary. AgentServer does not register them, record Runtime-owned single-Agent streams in its host task map, or cancel those streams a second time. Process CLI uses the same standard Runtime. The old constructor selection and Process-CLI-specific path were removed.

Web, TUI, ACP, Feishu, enterprise Feishu, Xiaoyi, WeCom, DingTalk, Telegram, Discord, Slack, WhatsApp, and WeChat keep their existing wire protocol and persistence format; channel modules do not acquire a second Session state. Their `ask_user` events leave a Runtime execution waiting after the output stream ends, and their answers resume that execution as control input instead of entering the work queue.

The verified-download asset owner now selects a portable per-process default on Windows and avoids directory `fsync`, allowing the Web channel suite to exercise the same path on Windows.

## Verification

After rebasing onto upstream `7f0c03836`, the Runtime and affected AgentServer/Gateway control-path selection passed 453 tests. The configured-provider gate also passed a real `ask_user` round whose first stream ended before the answer. The broad channel/Gateway/TUI selection passed 1277 tests with one skip on the original integration baseline; three unrelated Windows/frontend baseline groups remain outside this flow.
