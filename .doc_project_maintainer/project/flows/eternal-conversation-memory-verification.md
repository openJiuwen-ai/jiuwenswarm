---
id: eternal-conversation-memory-verification
name: Eternal Conversation Memory Verification
confidence: confirmed
last_updated: 2026-10-19
read_when: "Verifying Persist Session regressions, restart recovery, or 200-task acceptance evidence."
---

# Eternal Conversation Memory Verification

- Unit/regression: `tests/unit_tests/agentserver/rails/test_eternal_conversation_rail.py` and `test_eternal_conversation_deep_agents.py` cover lifecycle-safe replacement, exact Worker-prefix forking, the 70% token-window boundary, child Session lineage, one appended Extractor instruction, unified Skill staging, the real Builder helper chain, structural prefix/cache evidence, retries, batching, blob reconstruction, Builder responsibility, and worker-error recovery.
- Failed-boundary recovery regression (2026-10-15): 81 Rail tests and 18 DeepAgent tests pass. The focused cases verify that a failed Worker result creates only `task-failed`, a missing DeepAgent fork performs no background model call, and the next exact fork publishes through the retained cursor without losing intervening Raw History.
- Acceptance recovery regression (2026-10-15): all 79 Rail tests pass, including byte-identical first attempts, explicit failed-request invalidation on retry, checkpoint-baseline preservation, and invalidation recovery after runner restart.
- Large-session restart regression (2026-10-19): a resumed DispatchLab run exposed synchronous startup scans over 581 MB Raw History plus its 581 MB foreground mirror, exceeding the runtime heartbeat. Clean restarts now use bounded tail validation; disagreement still exercises full recovery. Six focused evidence/recovery tests pass, and the resumed real-model run crossed the former failure point from its original checkpoint.
- Real model: `scripts/acceptance/eternal_conversation_200.py` drives Web/TUI × Work/Code with a configured model and emits per-quadrant proof. Its background timeout is an inactivity limit renewed by cursor/revision or Agent-history progress. The current selected model is `Deepseek-V4-Flash-0731`.
- Accepted Web Code proof (2026-09-17): 200/200 tasks, 5/5 hidden probes, 49 replacements, 12829 verified Raw History records, 511/511 exact Extractor prefixes, 184 Built UTs, zero Pending, and final pytest return code 0.
- Accepted progress-aware Web Code proof (2026-09-24): 200/200 tasks, 5/5 hidden probes with retrieval, 56 replacements, 12377 verified Raw History records, 179 Built UTs, zero Pending, and final pytest return code 0. One predeclared edge-case no-op was rescored from preserved evidence without replay; the original failure remains in `rescore_audit`.
- Normative design: `docs/zh/永续会话Rail实现规范.md`.
