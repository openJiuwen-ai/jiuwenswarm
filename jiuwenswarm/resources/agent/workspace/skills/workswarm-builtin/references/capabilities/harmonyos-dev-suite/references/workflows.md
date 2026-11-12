# HarmonyOS Dev Suite Workflows

Use this reference after `harmonyos-dev-suite` triggers and the task needs a concrete workflow. Pick one workflow; avoid loading the full atomic catalog unless specialization is needed.

## Project Inspection

1. Locate the project root by looking for `build-profile.json5` or `oh-package.json5`.
2. Inspect modules through `module.json5` files.
3. Identify whether the task is ArkUI UI, ArkTS logic, native/C++, testing, device/debug, multi-device, service integration, or stability diagnosis.
4. Prefer read-only inspection before editing.

## ArkUI Feature Or Page Development

1. Confirm target module and page/component location.
2. Search unfamiliar APIs with the official HarmonyOS Developer Knowledge MCP when available. Call `searchDocuments` first and use the returned parent identifiers with `getDocumentsById` only when full content is needed; otherwise fall back to `devecocli docs search <keyword>`.
3. Follow existing project conventions for routing, state management, resources, and naming.
4. Implement the smallest cohesive change.
5. Validate with ArkTS syntax/build tooling when the user asks for verification or when edits are substantial.

Useful optional atomic Skills: `hmos-arkui-develop-skill`, `hmos-arkui-knowledge-retriever`, `hmos-arkui-mvvm-pattern`, `hmos-arkui-statemgt-migration`, `component_basic_ui`, `component_container`, `kits_ui`.

## ArkTS Logic Or API Work

1. Determine whether the question is syntax, API usage, deprecated API migration, or build failure.
2. Search or read official docs through the HarmonyOS Developer Knowledge MCP when available, with `devecocli docs` as the local fallback.
3. Keep examples compatible with ArkTS restrictions rather than generic TypeScript assumptions.
4. For code changes, preserve project style and run syntax/build validation when practical.

Useful optional atomic Skills: `hmos-arkts-knowledge-retriever`, `hmos-arkts-syntax-checker`, `hmos-arkts-deprecated-interface-checker`, `kits_arkts`, `lang-syntax`, `refactoring`.

## Build, Run, Device, Emulator, And Logs

Prefer these command families:

```bash
devecocli build
devecocli run
devecocli device list
devecocli emulator list
devecocli log --tail 200
```

Rules:

- Ask for a device name or serial when multiple devices are connected.
- Use timeouts for builds, logs, and emulator operations.
- Avoid following logs indefinitely unless explicitly requested.
- If DevEco Studio or SDK setup is broken, report the specific missing setup rather than retrying blindly.

Useful optional atomic Skills: `deveco-studio-hvigor`, `deveco-studio-emulator`, `deveco-studio-hilog`, `deveco-studio-verify`, `harmony-build-fix`, `harmony-verify`.

## Multi-Device Adaptation

1. Classify the issue: screen/window size, fold state, avoid areas, natural orientation, interaction methods, or hardware access.
2. Identify target device classes: phone, foldable, tablet, 2-in-1, wearable, TV, or multi-window.
3. Make layout and capability decisions explicit.
4. Validate visually or with device/emulator checks when possible.

Useful optional atomic Skills: `hmos-multidevice-scenario-entry`, `hmos-multidevice-screen-window-size`, `hmos-multidevice-fold-state`, `hmos-multidevice-avoid-areas`, `hmos-multidevice-natural-orientation`, `hmos-multidevice-interaction-methods`, `hmos-multidevice-hardware-access`.

## Stability And DFX Diagnosis

1. Identify artifact type: JS crash, C++ crash, freeze, API fault, rawheap/heapsnapshot, static memory leak risk, or generic logs.
2. Preserve raw logs and stack traces.
3. Extract signal, stack, lifecycle, thread, memory, and API error context before proposing fixes.
4. Provide root cause, evidence, and remediation plan separately.

Useful optional atomic Skills: `hmos-jscrash-analysis`, `hmos-cppcrash-analysis`, `hmos-appfreeze-analysis`, `hmos-apifault-analysis`, `hmos-jsleak-analysis`, `hmos-memleak-analysis`, `kits_performance`.

## Testing

1. Choose local tests for pure module/unit validation.
2. Choose instrument tests when a device or emulator is required.
3. Ask for module, suite, or single test case when the scope is ambiguous.
4. Report command, result, failing case, and next fix.

Useful optional atomic Skills: `hmos-local-test`, `hmos-instrument-test`, `kits_test`.

## Atomic Service And Application Services

1. Identify service family: Atomic Service, ASCF, Account Kit, Push Kit, Scan Kit, Live View Kit, or another kit.
2. Check permissions, configuration files, signing, domain/entitlement requirements, and platform limitations.
3. Provide integration steps plus minimal code snippets.
4. Include release or review constraints when relevant.

Useful optional atomic Skills: `hmos-atomicservice-assistant`, `hmos-ascf-assistant`, `hmos-ascf-convert-taro`, `hmos-ascf-convert-uniapp`, `hmos-account-kit-quicklogin-client`, `hmos-push-kit`, `hmos-scan-kit-defaultscan`, `hmos-scan-kit-customscan`, `hmos-live-view-kit-build-location`.
