---
name: harmonyos-dev-suite
description: Unified HarmonyOS development entry skill for ArkTS, ArkUI, DevEco CLI, build/run/debug, device logs, testing, multi-device adaptation, atomic services, native development, stability diagnosis, and optional HarmonyOS atomic Skill routing. Use when the user mentions HarmonyOS, OpenHarmony, HongMeng, 鸿蒙, ArkTS, ArkUI, DevEco, .ets, oh-package.json5, build-profile.json5, HAP/HAR/App, emulator, hilog, hdc, hvigor, or HarmonyOS app development.
---

# HarmonyOS Dev Suite

Use this skill as the default HarmonyOS development entrypoint. Keep the suite lightweight: route the task, prefer `devecocli` for tool operations, load only the reference needed for the current task, and install optional atomic Skills only when the user explicitly needs deeper specialization.

## Quick Triage

1. Detect project context before acting:
   - HarmonyOS project markers: `build-profile.json5`, `oh-package.json5`, `module.json5`, `.ets`, `.hml`, `.har`, `.hap`.
   - If no project is present, answer as guidance or ask for the target project path before running build/device commands.
2. Prefer `devecocli` over direct `hvigor`, `hdc`, emulator, or DevEco internals when a command is needed.
3. For documentation or API uncertainty, prefer the official HarmonyOS Developer Knowledge MCP when `searchDocuments` and `getDocumentsById` are available. Search first, then fetch only the specific full documents needed. Fall back to `devecocli docs search` or `devecocli docs read` when the remote MCP is unavailable.
4. For broad HarmonyOS tasks, read `references/workflows.md` and choose the smallest workflow that fits.
5. For a specialized area, read `references/atomic-skills-catalog.md` to identify an optional atomic Skill. Do not install optional atomic Skills silently.

## Routing

Use these routes:

- ArkUI UI/component/page work: use the ArkUI workflow in `references/workflows.md`; consult atomic Skills such as `hmos-arkui-develop-skill`, `hmos-arkui-knowledge-retriever`, `component_basic_ui`, `component_container`, `kits_ui`, or `hmos-design-visual-mobile` when specialization is needed.
- ArkTS language/API work: use the official knowledge MCP when available, otherwise use `devecocli docs search`; consult `hmos-arkts-knowledge-retriever`, `hmos-arkts-syntax-checker`, `hmos-arkts-deprecated-interface-checker`, `kits_arkts`, or `lang-syntax`.
- Build, run, emulator, log, or device tasks: prefer `devecocli build`, `devecocli run`, `devecocli device`, `devecocli emulator`, and `devecocli log`; consult DevEco atomic Skills only when the user needs their detailed workflow.
- Multi-device adaptation: route through `hmos-multidevice-scenario-entry`, then choose screen/window size, fold state, avoid areas, natural orientation, interaction methods, or hardware access.
- Stability and fault diagnosis: classify crash/freeze/leak/API fault first, then route to the matching DFX atomic Skill from the catalog.
- Testing: use `hmos-local-test` for local unit tests and `hmos-instrument-test` for device/emulator instrument tests.
- Atomic service or application service integration: consult catalog entries for ASCF, Atomic Service, Account Kit, Push Kit, Scan Kit, Live View Kit, and related kits.
- Native/C++ work: consult `deveco-native-flow` and its native/kits references when the task involves NDK, C/C++, NAPI, native build, or native crash analysis.

## Optional Atomic Skills

This suite indexes optional atomic Skills from the HarmonyOS Skills repository:

- Human-readable catalog: `references/atomic-skills-catalog.md`
- Machine-readable manifest: `assets/atomic-skills.json`
- Local installer helper: `scripts/install_atomic_skill.py`

Use the catalog when the suite needs a more specific specialist. Install only when the user asks for that specialist or when a task clearly requires repeated deep use of that atomic Skill.

Example local install:

```bash
python jiuwenswarm/resources/agent/workspace/skills/harmonyos-dev-suite/scripts/install_atomic_skill.py \
  --source /path/to/harmonyos-agent-skills \
  --target ~/.jiuwenswarm/agent/workspace/skills \
  --skill hmos-arkui-develop-skill
```

After installing an optional atomic Skill, reload JiuwenSwarm skills if the current runtime does not auto-refresh.

## Safety

- Do not modify or build a HarmonyOS project before identifying the target project path.
- Do not start emulators, install apps, follow logs, or run long build commands without clear user intent.
- Do not bypass `devecocli` with direct `hvigor`, `hdc`, or emulator commands unless `devecocli` is unavailable and the user accepts the fallback.
- Do not install all atomic Skills by default. The suite is the default entrypoint; atomic Skills remain optional.
- When using the official knowledge MCP, send focused search terms and retrieve full documents only when snippets are insufficient; `getDocumentsById` supports at most 10 documents per call.
- When installing from a local source, validate that the source directory contains a `SKILL.md` and that the destination remains under the configured skills directory.
