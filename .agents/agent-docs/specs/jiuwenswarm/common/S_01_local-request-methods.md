---
id: S_01_local-request-methods
title: JiuwenSwarm request method ownership
type: S
author: mingo007
author_email: maoqiming1@huawei.com
modules:
  - jiuwenswarm/common/schema
  - jiuwenswarm/common/e2a
files:
  - jiuwenswarm/common/schema/req_method.py
  - jiuwenswarm/common/schema/message.py
  - jiuwenswarm/common/schema/agent.py
  - jiuwenswarm/common/e2a/gateway_normalize.py
  - jiuwenswarm/common/e2a/agent_compat.py
---

# Local Request Method Contract

## Scope and Ownership

JiuwenSwarm owns `ReqMethod` in `common/schema/req_method.py`. Both
`schema.message.ReqMethod` and `schema.agent.ReqMethod` are aliases of this
class. `Message` and `AgentRequest` are local dataclasses whose `req_method`
type is `ReqMethod | str | None`. They are not aliases of e2a request models.

E2A envelopes, response models/codecs, identity models, events and modes remain
shared with agent-protocol. The standalone agent-gateway owns its internal
request types; its objects must not be compared by identity with Swarm enums.

## Invariants and Interfaces

- Existing member names and wire values are preserved. New Swarm methods are
  declared locally, without mutating the protocol enum or requiring a protocol
  version bump solely to publish the method catalog.
- `method_value`, `message_to_legacy_agent_dict`, `message_to_e2a`,
  `message_to_e2a_or_fallback` and `e2a_from_agent_fields` in
  `common/e2a/gateway_normalize.py` use the local enum. Method values become plain
  strings before constructing E2A envelopes or legacy snapshots.
- These outbound helpers preserve nonempty extension method strings. Explicit
  empty/whitespace strings raise `ValueError`; non-string values raise
  `TypeError`. An absent method remains absent. Fallback does not conceal an
  invalid method. Other normalization failures retain the legacy fallback path.
- `common/e2a/agent_compat.py`, Front ingress and IPC decode supported strings
  into the same local enum. Front ingress rejects unknown/non-string methods
  with `METHOD_NOT_FOUND`; this migration does not introduce unrestricted
  dispatch of arbitrary strings.
- Request field order/defaults, memory normalization, metadata, application
  identity and trusted server-only context semantics remain unchanged.
- JSON transport carries method strings, not Python enum or dataclass identity.
  External components must exchange wire data or explicitly use their own
  request types. Cross-package `isinstance` compatibility is not guaranteed.

## Change and Verification Entry Points

Adding a method still requires its handler, routing/forwarding configuration and
tests. Updating the protocol pin requires reviewing shared model changes against
the copied request fields; local copies do not update automatically.

- `tests/unit_tests/common/test_local_req_method.py`: all local methods through
  E2A/JSON/IPC, local annotations, legacy/fallback handling, isolated protocol
  catalog and request-converter removal.
- `tests/unit_tests/common/test_req_method_contract.py`: Gateway forwarded
  strings must be represented in the local catalog.
- `tests/unit_tests/agentserver/test_front_method_validation.py`: invalid method
  behavior at ingress.
- `docs/en/E2A-protocol.md` and `docs/zh/E2A-protocol.md`, section 4.4: wire boundary.
