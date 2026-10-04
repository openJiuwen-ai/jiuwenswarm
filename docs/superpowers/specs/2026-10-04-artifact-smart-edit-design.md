# Artifact Preview Smart Edit (产物预览智能编辑)

**Date:** 2026-10-04  
**Status:** Draft for review  
**Source parity:** agent-wb preview 「AI 编辑」(+ optional local MD style bar)  
**Host surface:** WorkSwarm web `ArtifactsPanel` (产物预览)

## Goal

Bring agent-wb’s preview-panel **AI edit** (select text → instruct agent) into WorkSwarm’s artifact preview, then add **local text editing** (save + markdown style bar). Do both in-repo by porting logic into React; do not embed agent-wb.

## Decisions (locked)

| Topic | Choice |
| --- | --- |
| Scope | Phase A (AI edit) then Phase B (local edit + MD style bar) |
| File types for selection AI | Align agent-wb: markdown, text, code/json, html, docx, xlsx, pptx |
| Submit mode | Default **auto-send**; setting for **fill-only** |
| Local edit (Phase B) | Text kinds editable + save; MD/text style bar; **no** Office binary write-back |
| Missing workspace `path` | AI edit still allowed (name + quote); local save disabled |
| Integration approach | Port selection + chat bridge into WorkSwarm (not iframe / not weak paste-only) |

## Non-goals (v1)

- New backend `/smart-edit` REST API
- Embedding agent-wb via iframe / microfrontend
- Office (docx/xlsx/pptx) binary write-back
- Selection AI inside desktop built-in browser path (`openFileInDesktopBrowser`)
- PDF / image pixel editing; pdfkit `smart_edit` skill (orthogonal)

## Architecture

```
产物预览 FilePreview (by previewKind)
        │ user selects text
        ▼
previewSelection (ported from agent-wb)
  → DocSelection { kind, source, path?, quote, range? }
        │
        ├─「AI 编辑」→ user instruction
        │       ▼
        │  previewAiEditRequest (chat store / bridge)
        │       ▼
        │  InputArea consumes
        │       ├─ auto_send (default): compose prompt → send
        │       └─ fill_only: setInputValue only
        │
        └─ markdown/text style bar (Phase B)
                ▼
           local buffer → POST /file-api/file-content (path required)
```

### Prompt contract

WorkSwarm uses a text channel (not agent-wb ACP `selection-quote` blocks). Compose a single user message:

- **With path:**  
  `@file:{path} [range]\n> {quote}\n\n{user instruction}`
- **Without path:**  
  `文件「{name}」[range]\n> {quote}\n\n{user instruction}`  
  Agent locates the file itself; local save remains disabled.

Quote length is capped (parity with agent-wb: ~4000 display / enough model context).

### Component boundaries

| Layer | Responsibility |
| --- | --- |
| `ArtifactsPanel/FilePreview` + `ArtifactExpandedPanel` | Selection float, style bar, edit/save chrome, gating |
| `ArtifactsPanel/previewSelection*` (+ `docSelection` types) | Selection model, caps, kind gating (ported) |
| `previewAiEdit` bridge (store or feature module) | One-shot request; InputArea clears after consume |
| `InputArea` | Compose + auto-send / fill-only per preference |
| Existing `file-api` | Phase B save only; no new smart-edit API |
| Optional extract from `AgentPanel/FileViewer` | Shared save helper to avoid duplicate write paths |

## UI / UX

### Phase A — selection float

- On supported kinds, selecting non-empty text shows a float near the selection.
- Primary action: **AI 编辑** (i18n).
- Expands to a mini prompt + **Send**.
- After send: clear selection, dismiss float; honor auto-send vs fill-only.
- Missing path: AI edit still enabled; no local-save affordances.
- Switching artifact / leaving preview: discard unsent AI-edit draft.

### Phase B — toolbar + style bar

- Expanded toolbar: **Edit / Done**, **Save** (dirty only), only for markdown / text / code / json **and** writable `path`.
- Edit mode: CodeMirror / text surface writable.
- markdown + text: float may include local style buttons (bold / italic / code / link) mutating local buffer only.
- html + Office: AI selection only (no style bar, no save).
- image / pdf / video / unsupported: no selection float.
- Leaving preview or switching file with dirty buffer: confirm discard (agent-wb D-38 spirit).

### Visual / a11y

- Use WorkSwarm theme tokens (`--color-*`); do not import agent-wb palette.
- `data-testid` prefix `artifact` (see web AGENTS.md), e.g. `artifact-selection-float`, `artifact-ai-edit-btn`, `artifact-ai-edit-input`, `artifact-ai-edit-send`, `artifact-edit-toggle`, `artifact-save`.

## Settings

- **Location:** Settings → General.
- **Key:** Artifact AI edit submit mode (`auto_send` | `fill_only`).
- **Default:** `auto_send`.
- **Storage:** frontend preference (localStorage / existing preferences channel); no new backend config field.
- **Scope:** Phase A submit path only; local style/save unaffected.

## Kind gating matrix

| previewKind | Selection float | AI edit | Local style bar | Local edit + save | No path |
| --- | --- | --- | --- | --- | --- |
| markdown | yes | yes | yes | yes if path | AI ok; save disabled |
| text | yes | yes | yes | yes if path | same |
| code / json | yes | yes | no | yes if path | same |
| html | yes | yes | no | no | AI ok |
| docx / xlsx / pptx | yes (rendered text) | yes | no | no | AI ok |
| image / pdf / video / unsupported | no | no | no | no | — |

### Selection rules

- Empty / whitespace-only selection: no float.
- Office: quote visible rendered text + optional range label (e.g. current slide / selection); no precise cell/paragraph coordinates in v1.
- Desktop browser short-circuit for html/md: out of scope for selection edit in v1.

### Failure / busy session

- Local save failure: surface error; keep edit buffer.
- Auto-send while session busy: reuse existing queue / interrupt behavior; no side channel.

## Phased delivery

1. **Phase A:** selection model + float + AI edit bridge + General setting + InputArea consume.
2. **Phase B:** editable text kinds + save via file-api + MD/text style bar + dirty switch confirm.

## File touch list (expected)

**Add**

- `jiuwenswarm/channels/web/frontend/src/components/ArtifactsPanel/previewSelection.ts` (and docSelection helpers as needed)
- `.../SelectionFloat.tsx`
- `.../previewTextEdit.ts`
- `.../src/features/artifactAiEditPreference.ts`
- `.../src/features/previewAiEditBridge.ts` (or fold into chat store)

**Modify**

- `FilePreview.tsx`, `CodePreview.tsx`
- `ArtifactExpandedPanel.tsx`
- `ChatPanel/InputArea.tsx` (send path)
- `features/settings/modules/general/GeneralSettings.tsx`
- `i18n/locales/zh.json`, `en.json`
- Optionally factor save helper from `AgentPanel/FileViewer.tsx`

## Test plan

- Unit: selection build/cap, prompt compose with/without path, kind gating, preference read/write, style wrap.
- Component/integration: float show/hide; auto_send vs fill_only; save disabled without path; dirty switch confirm.
- Manual: one AI-edit path each for md / code / html / docx / xlsx / pptx; MD local bold + save; busy-session queue behavior.

## Open follow-ups (explicitly deferred)

- ACP-style structured selection blocks if WorkSwarm chat gains rich content blocks later.
- Office write-back.
- Selection edit inside desktop browser tab.
- pdfkit agent-side `smart_edit` skill packaging.
