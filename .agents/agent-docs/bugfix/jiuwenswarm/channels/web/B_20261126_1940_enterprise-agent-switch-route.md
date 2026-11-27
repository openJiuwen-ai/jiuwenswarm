---
id: B_20261126_1940_enterprise-agent-switch-route
title: Open a new conversation after an enterprise Agent switch
type: B
author: linda
author_email: weipingtao@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/EnterpriseEntry.tsx
  - jiuwenswarm/channels/web/frontend/tests/enterpriseUserEntry.test.mjs
designs: []
design_note: Minimal fix explicitly approved in the task; no separate design document.
---

# Enterprise Agent switch retains the previous conversation

The initial implementation and verification below describe the original baseline.
The 2026-11-27 revision at the end defines the current scope after upstream changes.

## Symptom and cause

Switching from Agent A while viewing `/chat/<session-A>` replaced the identity
query parameters but retained the pathname. Agent B then queried A's session ID.
When metadata returned `NOT_FOUND` and the session was absent from B's list,
App displayed the conversation-not-found page.

## Decision and scope

Only explicit listed/custom Agent switches navigate to `chatRoutePath({ kind:
'chat-new' })` after active-cluster selection succeeds. The existing route builder
preserves the deployment base path. Bootstrap retains the current pathname so
refreshes and direct session links continue to work. Failed switches retain the
current conversation and report the existing error. Identity parameters, debug
context handling, and URL fragments retain their existing behavior.

Automatically restoring the target Agent's last session was rejected because it
requires scoped persistence and additional missing-session recovery. Changing the
global missing-session page was rejected because genuine invalid links must still
report missing sessions. No backend or public protocol change is required.

## Verification

Baseline: `9aa2ba28dd65195586743de0a45c6f6191ceb548`.
The component suite renders the real EnterpriseEntry with React/JSDOM, mocks
Identity/Manager responses, and observes navigation from its context callbacks.
It covers listed/custom switches, target identity and cluster selection, switch
failure, direct session links, authentication failures, personal edition, and
simulated login. Navigation is captured instead of performing a browser reload.

`node --test tests/enterpriseUserEntry.test.mjs`: 11/11 passed.
`TEST_APP_BASE=/swarm node --test tests/enterpriseUserEntry.test.mjs`: 11/11 passed.
Running the same tests with the baseline EnterpriseEntry source supplied through
an in-memory esbuild loader produced exactly two expected failures: listed and
custom switches from an existing session retained `/chat/existing-session`.
The other nine tests passed against the baseline.

Environment: Node 26.0.0, React/JSDOM, existing local frontend dependencies.
The missing declared `oidc-client-ts@3.5.0` dependency was installed in a temporary
directory and linked into ignored node_modules; no manifest or lockfile changed.
The fixture shares the i18next instance and compares translated empty-state text,
instead of assuming the translation key is rendered.
Production enterprise deployment and full browser reload behavior were not tested.

## Review

The final source/test diff was reviewed against the baseline using jw-review.
Both switch callbacks, bootstrap, active-cluster failure, scoped identity, and
the existing BASE_URL-aware route builder were checked. No confirmed regression
was found within this scope. No UI markup, public protocol, dependency declaration,
or backend behavior changed. Root/Web/frontend AGENTS and the existing WebUI
client-capability contract were consulted. Real browser navigation remains an
integration validation limit.

## Contract / 契约

Explicit Agent switches start a new conversation in the target context; initial
loads preserve direct session links. 主动切换 Agent 后进入目标上下文的新对话；首次加载保留会话链接。

## 2026-11-27 upstream compatibility revision

Upstream `acc6c1afe9a73b9f0d5b6e682399e93ffa6bb301` removed custom Agent
switching, added Agent identity fields, and expanded empty-state handling/tests.
Replaying this fix conflicted in EnterpriseEntry and its test suite.

Keep upstream identity fields, empty-state behavior, and removal of custom switching.
Only the remaining listed-Agent switch supplies the new-conversation path.
Remove obsolete custom-switch tests and exercise the listed switch for failure
handling. Preserve upstream English translation fixtures and both empty-state tests.
This supersedes the custom-switch scope above; initial verification remains historical.

After resolution, both `node --test tests/enterpriseUserEntry.test.mjs` and
`TEST_APP_BASE=/swarm node --test tests/enterpriseUserEntry.test.mjs` passed 10/10.
The same tests with unmodified upstream EnterpriseEntry supplied by an in-memory
esbuild loader failed exactly the existing-session switch case; nine others passed.
The declared missing OIDC dependency was restored in the same temporary directory.
No dependency declarations or lockfiles changed. Real enterprise browser reload
and full-repository testing remain unverified.

## Extended regression verification (2026-11-27)

- Entry component: 10/10 passed at root, 10/10 with `/swarm` base path.
- `enterpriseAgentContext.test.mjs`: 5/5 passed, including strict combobox and
  the upstream switcher without custom mode.
- `test:new-conversation-entry-settings`: 4/4 passed, covering draft/default
  model and collaboration model-group preservation.
- `test:auth-session`: 12/13 passed. The OIDC federation callback test expects
  `/portal/auth/federation/keycloak/callback`, while actual routing uses
  `/auth/federation/keycloak/callback`.
- `appRequestContext.test.mjs` fails during setup because its source-extraction
  marker `const configInitializationRef = useRef<` no longer exists in App.

Both failures were independently reproduced in a clean detached worktree of
upstream `acc6c1afe9a73b9f0d5b6e682399e93ffa6bb301`, with identical errors.
Neither the failing tests nor their production modules change in this PR.
They remain unresolved upstream validation gaps; the extended suite is not
reported as fully passing. Fixing them is outside this minimal route repair.
No Chinese characters were found in added production/test source lines.
Final review preserves one repair commit and the original five-file scope.
