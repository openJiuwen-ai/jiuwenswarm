---
id: F_20261114_1714_local-request-methods
title: Localize ReqMethod and dependent request models
type: F
author: mingo007
author_email: maoqiming1@huawei.com
modules:
  - jiuwenswarm/common/schema
  - jiuwenswarm/common/e2a
  - jiuwenswarm/extensions/video_duplex
files:
  - jiuwenswarm/common/schema/req_method.py
  - jiuwenswarm/common/schema/message.py
  - jiuwenswarm/common/schema/agent.py
  - jiuwenswarm/common/e2a/gateway_normalize.py
  - jiuwenswarm/extensions/video_duplex/backend/task_adapter.py
  - jiuwenswarm/extensions/video_duplex/extension.py
  - tests/unit_tests/common/test_local_req_method.py
  - tests/unit_tests/common/test_req_method_contract.py
designs: []
design_note: Explicit user request to copy ReqMethod-related implementation into JiuwenSwarm; no separate design document.
---

# Local Request Method Implementation

## Basis and Decisions

Base: `a693d6a0c` on `develop`. Candidate: `feature/local-reqmethod`, PR 10186,
including the local enum commits `709a408e7` and `a8cd02903` plus this completion.
Current contract: [S_01_local-request-methods](../specs/jiuwenswarm/common/S_01_local-request-methods.md).
No earlier feature record owns this migration. This explicitly replaces shared
request-enum ownership, while preserving the shared E2A wire protocol. The
Gateway/Server wire boundary recorded in F_20261031_1139_gateway-session-e2a
remains in effect.

The source inventory identified the enum in `e2a/message.py`, the dependent
`Message` and `AgentRequest` models in `message.py` and `agent_models.py`, and
five outbound request helpers in `gateway_normalize.py`. Copying only the enum
left model annotations and request normalization tied to e2a, so those dependent
definitions were also copied. The existing local inbound compatibility adapter
already resolves methods through the local schema imports.

The local catalog contains 543 methods: all 434 methods from the pinned protocol,
all previous Swarm runtime extensions (532 unique methods combined), and 11
additional Gateway-forwarded methods. Names and wire values were compared
against the pinned source and the base commit's `_ensure_req_method` calls.
Request dataclass field order, defaults, factories and comparison/repr flags
match the pinned e2a models. The local enum retains string-compatible values.

Swarm no longer patches or imports the e2a request enum. Request conversion
produces plain wire strings, including legacy fallback snapshots. Response
normalization and Team delta identity behavior remain shared/unchanged. The
video-duplex capability check and task adapter use the local request code.
No protocol/Gateway dependency pins or other repositories were changed.

Gateway-only tests use `agent_gateway.common.protocol_methods.ReqMethod`, since
the pinned external Gateway still performs enum identity checks internally.
Server-side fixtures use local models. Passing a Swarm enum directly to a
Gateway-only Python API is not the production wire boundary and broke metadata,
chat/cancel and trace assertions during verification; correcting fixture ownership
restored these paths without changing external Gateway behavior.

## Validation on 2026-11-14

Environment: Windows, Python 3.12.14 in `../.venv-e2a`. `PYTHONPATH` selected this
checkout and extracted sources for the repository's pinned e2a
`06a75d66eb011762077e8924d0d1e41791a93034` and agent-gateway
`2c40ac150276477a8079ef7937674b600b6a1420`. This avoids the unrelated editable
Gateway checkout. Other installed dependencies are not a fully locked environment.

Final focused invocation, with the pinned source paths on `PYTHONPATH`:

```powershell
../.venv-e2a/Scripts/python.exe -m pytest `
  tests/unit_tests/common/test_local_req_method.py `
  tests/unit_tests/e2a `
  tests/unit_tests/agentserver/test_front_method_validation.py `
  tests/unit_tests/common/test_enterprise_protocol_identity.py `
  tests/unit_tests/server/jiuwenmate/test_split_gateway_contract.py `
  jiuwenswarm/extensions/video_duplex/tests/backend/test_extension_compatibility.py `
  tests/unit_tests/gateway/test_e2a_proxy.py `
  tests/unit_tests/gateway/test_swarmflow_reply_forward.py `
  tests/unit_tests/gateway/test_session_input_forwarding.py `
  tests/unit_tests/gateway/test_unknown_team_mode_rejection.py `
  tests/unit_tests/gateway/test_message_handler_evolution.py `
  tests/unit_tests/gateway/test_message_handler_stream_cancel.py `
  tests/unit_tests/process_cli/test_public_contract.py `
  tests/unit_tests/gateway/test_message_trace_pipeline.py `
  tests/unit_tests/gateway/test_cron_scheduler.py `
  tests/integration/gateway/test_gateway_message_trace.py `
  tests/unit_tests/gateway/test_hub_catalog_e2a.py `
  -k 'not test_three_provider_wire_acceptance_and_request_isolation' `
  -o addopts='' --asyncio-mode=auto -o log_cli=false -q --tb=short --maxfail=3
```

Result: **1229 passed, 3 deselected**, 66.84 seconds. The excluded parameterized
trace test was tried earlier: sampled/unsampled cases failed because installed
OpenTelemetry 1.45.2 emitted trace flags `03`/`02` versus expected `01`/`00`.
The installed SDK exceeds this repository's declared `<1.42` upper bound. No
telemetry behavior or test expectation was changed for this migration.

New regression coverage exercises every local method through E2A -> JSON ->
AgentRequest -> IPC, plus legacy/fallback Front ingress, invalid and absent
methods, and a subprocess with the protocol enum emptied and its request
converters disabled. Gateway initialization precedes that isolation because the
external package legitimately owns its own enum. Tests use controlled transports;
this is not a live deployed Server/Gateway end-to-end acceptance test.

## Coverage and Remaining Work

Reviewed the production imports, copied models/helpers, base-to-candidate enum
values, Gateway/Server fixture boundaries and video adapter call sites. Updated
the local contract, Chinese/English E2A section 4.4 and generated document indexes.
No formatter or static-analysis tool was run, per repository instructions.

Full TUI/method-forwarding contract, heartbeat, login Server and video task-bridge
tests could not complete in this environment: core imports require missing
`pdfplumber` and/or `alembic`. Dependency installation was attempted but stopped
after a stalled download. These tests and the excluded telemetry cases must be
rerun in the locked CI environment; no full-suite success is claimed. Protocol
pin changes require reviewing copied request models and repeating boundary tests.

Additional Gateway ACP/login run: 59 passed, 3 deselected, with one TUI-related
test failing on the same missing `pdfplumber` import. The Web handler suite also
could not collect because `a2ui` is absent. These results are separate from the
1229-case focused run above and are not reported as full-suite success.
