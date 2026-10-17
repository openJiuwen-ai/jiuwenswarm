---
name: dynamic-memory-cli
description: Operate a dynamic, UT-backed agent memory with atomic Pending publication, merged Pending/Built search, Memory Snapshot and history cursor state, Built-only validation, and exact-content migration. Use when an agent Harness must extract memory asynchronously while foreground work continues, publish immediately searchable Pending UTs, build them into a stable index, or inspect and recover an eternal-conversation memory project.
---

# Dynamic Memory CLI

Use `scripts/dynamic_memory_cli.py` as the deterministic boundary between a conversation Harness and its memory state. Keep semantic selection in the extraction Agent; keep validation, revisions, cursors, publication, search, and migration in this CLI.

## Required workflow

1. Run `init --path <session-memory-dir>` once.
2. Let the extraction Agent read the old Snapshot, frozen Working Memory, published UTs, and Raw History evidence.
3. Submit one proposal with `publish-pending --file <proposal.json>`. Never expose an Agent's scratch buffer directly.
4. Use `search` immediately. Published Pending UTs participate through lexical matching and are marked `pending` in results.
5. Run `freeze-pending --output <batch.json>` for a stable exact-content batch.
6. Let the builder Agent review that batch, then run `build-pending --file <batch.json>`.
7. Treat a successful build as an atomic representation migration. It does not advance semantic memory revision, Snapshot revision, or history cursor.

Read [references/dynamic-memory-contract.md](references/dynamic-memory-contract.md) before creating proposals or integrating a Harness.

## Command contract

Run from the memory project directory unless `init --path` is used:

```text
python scripts/dynamic_memory_cli.py init --path <dir>
python scripts/dynamic_memory_cli.py search <query> [query...]
python scripts/dynamic_memory_cli.py list [--full]
python scripts/dynamic_memory_cli.py show <id>
python scripts/dynamic_memory_cli.py check-conflicts --file <candidate.json>
python scripts/dynamic_memory_cli.py add --file <ut.json> [--force]
python scripts/dynamic_memory_cli.py update <id> --file <updates.json>
python scripts/dynamic_memory_cli.py retire <id> [--reason <text>]
python scripts/dynamic_memory_cli.py get-state
python scripts/dynamic_memory_cli.py publish-pending --file <proposal.json>
python scripts/dynamic_memory_cli.py freeze-pending --output <batch.json>
python scripts/dynamic_memory_cli.py build-pending --file <batch.json>
python scripts/dynamic_memory_cli.py test [--built-only]
python scripts/dynamic_memory_cli.py bench
```

Use the global `--root <session-memory-dir>` option before the subcommand when the caller cannot change its working directory.

Do not edit `memory.sqlite3` manually. Preserve stable UT IDs. Modifying a Built UT must republish it as Pending. Do not advance a cursor unless Snapshot and UT changes are committed in the same `publish-pending` transaction.

## Agent boundaries

- Extraction Agent: decide what to remember, resolve semantic conflicts, author UTs, and produce Snapshot content.
- Builder Agent: review a frozen Pending batch and request deterministic construction and Built-only validation.
- Harness: verify identity/range/version inputs and invoke atomic commands.
- CLI: enforce schemas, continuity, exact-content comparison, transactions, search merging, and test gates.

Save each foreground, extractor, and builder transcript outside the database in the session's Raw History or agent-history area. Store evidence references on every changed UT and on empty-UT proposals.
