# Project Maintainer Docs

Status: partial
Last updated: 2026-08-01
Sync status: partial

## Project Brief

- Name: JiuwenSwarm
- Purpose: Multi-agent collaboration runtime with web, TUI, CLI, IM, ACP, A2A, distributed team, skill, memory, and sandbox capabilities.
- Primary users: Developers and teams using agent orchestration, multi-channel assistants, and task automation.
- Main runtime: Python package with AgentServer and Gateway processes, plus React/TypeScript frontends.
- Tech stack: Python 3.11+, websockets, FastAPI, OpenJiuwen, pytest, TypeScript/React, package and installer scripts.

## How To Read

Start with `INDEX.md`, then `manifest.yaml`, then the module, directory, flow, or code symbol relevant to the task.

The delivery prioritizes `agentserver` runtime analysis. The frozen authoritative inventory contains 1,275 source files and 15,584 required symbols. A fresh comparison scan at `10afedf2` observed 1,285 files and 15,809 symbols without widening that ledger. All 128 existing `AgentWebSocketServer` methods have cards and remain `agent_audited`, with 0 source-expired; current integrity verification trusts 59 and flags 69 entry-document hash mismatches. The 823-method frozen server queue excludes 6 newly observed unaudited methods. Eight AgentServer flows are documented; broader repository coverage remains partial.

## Maintenance Rules

- Keep files within the size budgets in the Project Maintainer skill.
- Use `project/build-plan.md` as the operational queue for pending slices.
- Keep `project/coverage-map.json` and `project/symbol-audit-map.json` summaries current after inventory refreshes.
- For very large ledgers, store full generated JSON in compressed machine files and keep the project JSON files as navigable summaries.
- Do not mark this artifact `current` until stable tracked paths are mapped or out of scope, every required source symbol has an entry doc with `Actual Role` and health, required flows are documented or out of scope, and requested-scope audit symbols are audited or out of scope.
- Treat `audit.status: unaudited` as pending even when a symbol card has a useful first-pass behavior summary.
- Expire Python audits by normalized AST symbol hash. A changed method expires itself and its class audit without expiring unchanged siblings; legacy file-hash records remain conservative when safe migration is impossible.
