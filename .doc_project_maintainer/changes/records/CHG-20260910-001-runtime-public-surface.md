---
id: CHG-20260910-001
title: Narrow the Session Runtime public surface
type: refactor
date: 2026-09-10
modules:
  - runtime-session
  - agentserver-runtime
flows:
  - runtime-session-reference-chain
confidence: confirmed
---

# Narrow the Session Runtime Public Surface

## What Changed

`jiuwenswarm.runtime` now exposes only the product entry point `AgentRuntime` and its lifecycle error `RuntimeStateError`. Session Coordinator, execution models, Runtime context, and request-resolution helpers are no longer duplicated through the package root. Provisioning callers import create/switch/fork/delete transaction contracts from the dedicated `jiuwenswarm.runtime.session_provisioner` submodule.

The Coordinator accessor and the Session adoption, ownership, mode-classification, and chat-preparation helpers on `AgentRuntime` are internal implementation details. Session delete commits its provisioning transaction internally instead of exposing a second public commit method. No compatibility alias or fallback path was retained.

This is an API-boundary refactor only: Session scheduling, generation, cancellation, control delivery, persistence, and transport behavior are unchanged.

## Verification

After rebasing onto upstream `029c76a64`, the repository-locked `.venv` passed 456 affected deterministic tests across Process CLI, Runtime architecture and service, Coordinator, reference-chain, provisioning, AgentServer ACP and lifecycle boundaries. The four configured-model system tests then passed on the same rebased source. Compilation, Ruff checks for all changed Python files except the pre-existing AgentServer lint debt, and `git diff --check` passed. The AgentServer file continues to report unrelated existing E402/F841 findings outside the changed import block.

A subsequent full affected-scope run executed all Runtime, Process CLI, AgentServer, Gateway, Web/IM channel, TUI, ACP, and E2A unit directories. It passed 5,937 unit cases; the 28 failures were traced to unchanged Windows/path, optional-dependency, asset-permission, CLI-encoding, and AgentManager cleanup baseline code. All 72 focused Web Session JavaScript tests and the complete TUI build/test suite passed. The configured-model gate then passed all four Work, Code, chat ask-user, and Goal ask-user cases, and a real AgentServer/Gateway WebSocket roundtrip passed with only a Windows locked-log teardown error. The full Web frontend build remains blocked by the upstream `onnxruntime-web/wasm` resolution failure. Live third-party IM vendor APIs were not exercised because provider accounts were unavailable; their repository integration and contract suites passed.
