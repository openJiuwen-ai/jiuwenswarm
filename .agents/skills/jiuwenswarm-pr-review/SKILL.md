---
name: jiuwenswarm-pr-review
description: Review a specified JiuwenSwarm pull request against the repository's technical direction, architecture, feature interactions, documentation, scope, code quality, reuse, and future evolution. Use for PR review or re-review only.
---

# JiuwenSwarm PR Review Standards

This skill defines review criteria for the scope specified by the caller. Use the relevant dimensions below, the applicable parts of the [repository technical standards](references/technical-route.md), and the project instruction files governing changed paths. Within a directory, `AGENTS.override.md` takes precedence over `AGENTS.md`; more specific directory guidance takes precedence over broader guidance. Judge behavior against the target branch, the implementation, tests, and applicable design decisions. Treat a defect as PR-introduced when the change creates it or makes an existing problem materially worse; otherwise, treat existing debt as context unless the change depends on it.

## Technical direction and architecture

- Check module responsibilities, dependency direction, sources of truth, resource ownership, public contracts, and approved migration plans. A newly introduced architecture conflict is a serious risk. An intentional architecture change needs a documented rationale, affected call paths, a migration or retirement plan, and verification. Existing transitional code is not automatic justification for copying it.
- Check for bypasses of Runtime, Gateway/AgentServer, Channel, permission, and persistence boundaries; parallel implementations; competing authorities for the same state; and duplicated lifecycle control. Apply only standards relevant to the changed code.
- Check interactions with affected modes, channels, configuration, tools, sessions, message protocols, and error paths. Identity, state, and behavior for a shared concept should remain consistent across entry points unless a difference is justified.
- For agent changes, apply the openJiuwen Harness and Rail standards in the technical reference. Inspect the actual assembled Rail chain and the pinned openJiuwen contract, including mode-specific registration, lifecycle, priority, reload, permissions, and team-member providers.

## Review dimensions

1. **Overall architecture:** Does the change conflict with the current architecture or an approved migration direction? Are boundaries, dependencies, and ownership clear?
2. **Other features:** Does it disrupt adjacent features, modes, or channels, especially shared session, event, permission, or configuration semantics?
3. **Documentation coverage:** Are affected user, developer, API, configuration, operations, migration, example, and index documents maintained? Identify a concrete gap before treating the absence of a documentation change as a defect.
4. **Documentation versus code:** Do documented interfaces, defaults, states, errors, and examples match the implementation? Distinguish implemented behavior from planned behavior.
5. **Code versus claims:** Are the PR title, description, linked requirement, design, and test-plan claims implemented and verified? Identify omitted, hidden, or contrary behavior.
6. **Scope:** Does the PR include unrelated refactors, dependencies, lockfiles, configuration, generated assets, public APIs, or default behavior changes? Is any necessary expansion explained and reviewed for impact?
7. **Code standards:** Are responsibilities, names, types, errors, boundary cases, complexity, readability, and applicable language and directory lint, formatting, and test conventions sound?
8. **Reuse:** Does the change reuse existing protocols, shared logic, components, Rails, tools, and lifecycle entry points? Does a new abstraction have a distinct responsibility, or create a second path?
9. **Future evolution:** Does the change introduce coupling, global state, permanent compatibility branches, or hard-to-migrate data contracts that obstruct a known refactor or feature direction? Distinguish established migration blockers from speculative risks.

## Additional checks

- **Correctness and regression:** Check affected normal, boundary, failure, retry, cancellation, concurrency, shutdown, and recovery paths. Tests should prove externally observable contracts, not merely internal wiring.
- **Compatibility and migration:** Check APIs, E2A and event contracts, storage, configuration, older clients, and version transitions. Intentional breaking changes need an explicit decision and migration plan.
- **Security and resources:** Check permissions, untrusted input, sensitive data, file and network access, and ownership and cleanup of tasks, connections, locks, and background jobs.
- **Operations and delivery:** Where affected, check performance, observability, dependency locks, builds, packaging, platform support, and deployment behavior. A passing unit test does not override an applicable higher-level test or CI failure.
