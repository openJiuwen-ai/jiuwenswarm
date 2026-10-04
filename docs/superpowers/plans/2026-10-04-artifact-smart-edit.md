# Artifact Preview Smart Edit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port agent-wb preview 「AI 编辑」into WorkSwarm `ArtifactsPanel`, then add local text edit + markdown style bar, without embedding agent-wb.

**Architecture:** Pure selection/prompt helpers + a one-shot `previewAiEdit` bridge into `InputArea` (auto-send or fill-only). Phase B flips text previews writable and saves via existing `POST /file-api/file-content`. Office/html stay selection→AI only.

**Tech Stack:** React + TypeScript, CodeMirror (existing), Vite frontend, node:test + esbuild/tsc test scripts, localStorage preferences, WorkSwarm chat `onSubmit` path.

**Spec:** `docs/superpowers/specs/2026-10-04-artifact-smart-edit-design.md`

## Global Constraints

- Chrome/Chromium 107+ baseline for new/changed frontend code.
- Theme colors via semantic tokens (`var(--color-*)` / Tailwind semantic classes); no hardcoded product palette.
- New UI strings in both `zh.json` and `en.json`.
- `data-testid` prefix `artifact` for ArtifactsPanel elements (see `jiuwenswarm/channels/web/AGENTS.md`).
- No new backend smart-edit API; no Office binary write-back; no iframe embed of agent-wb.
- Prefer TDD for pure helpers; commit after each task.
- Working directory for frontend commands: `jiuwenswarm/channels/web/frontend`.

---

## File structure (locked)

| File | Responsibility |
| --- | --- |
| `src/components/ArtifactsPanel/docSelection.ts` | `DocSelection` type, caps, describe, switch-confirm |
| `src/components/ArtifactsPanel/previewSelection.ts` | Kind gating, build selection, DOM read, float position, prompt compose |
| `src/components/ArtifactsPanel/previewTextEdit.ts` | Local MD/text wrap helpers (Phase B) |
| `src/components/ArtifactsPanel/SelectionFloat.tsx` | Floating AI-edit UI (+ style buttons in Phase B) |
| `src/features/artifactAiEditPreference.ts` | `auto_send` / `fill_only` localStorage |
| `src/features/previewAiEditBridge.ts` | One-shot request pub/sub for InputArea |
| `src/components/ArtifactsPanel/FilePreview.tsx` | Wire selection root + edit buffer props |
| `src/components/ArtifactsPanel/CodePreview.tsx` | Optional editable mode |
| `src/components/ArtifactsPanel/ArtifactExpandedPanel.tsx` | Edit/Save toolbar + dirty confirm |
| `src/components/ChatPanel/InputArea.tsx` | Consume bridge |
| `src/features/settings/modules/general/*` | Setting row |
| `src/i18n/locales/{zh,en}.json` | Copy |
| `tests/artifactSmartEdit.test.mjs` | Unit tests for pure modules |
| `package.json` | `test:artifact-smart-edit` script |

Reference implementations (read-only, do not import across repos):

- `/home/shigp/workspace/agent-wb/frontend/src/lib/docSelection.ts`
- `/home/shigp/workspace/agent-wb/frontend/src/lib/previewSelection.ts`
- `/home/shigp/workspace/agent-wb/frontend/src/lib/previewTextEdit.ts`

---

### Task 1: Selection model + prompt compose

**Files:**
- Create: `jiuwenswarm/channels/web/frontend/src/components/ArtifactsPanel/docSelection.ts`
- Create: `jiuwenswarm/channels/web/frontend/src/components/ArtifactsPanel/previewSelection.ts`
- Create: `jiuwenswarm/channels/web/frontend/tests/artifactSmartEdit.test.mjs`
- Modify: `jiuwenswarm/channels/web/frontend/package.json` (add test script)

**Interfaces:**
- Consumes: `PreviewKind` from `./filePreviewModel`
- Produces:
  - `type SelectionKind = 'doc' | 'slide' | 'sheet' | 'html' | 'markdown' | 'text'`
  - `type DocSelection = { kind: SelectionKind; source: string; path: string; range: string; preview: string }`
  - `SELECTION_PREVIEW_CAP = 4000`
  - `capSelectionPreview(text: string, max?: number): string`
  - `needSwitchConfirm(current: DocSelection | null, next: DocSelection): boolean`
  - `supportsPreviewSelection(kind: PreviewKind): boolean`
  - `isPreviewStyleEditable(kind: PreviewKind): boolean`
  - `isPreviewLocallyEditable(kind: PreviewKind): boolean`
  - `buildDocSelection(input: { kind: PreviewKind; path: string; title?: string; selectedText: string; range?: string }): DocSelection | null`
  - `composeAiEditPrompt(sel: DocSelection, instruction: string): string`
  - `readDomSelectionText(sel: Selection | null | undefined): string | null`
  - `floatingBarPosition(...): { top: number; left: number }`

- [ ] **Step 1: Write the failing test**

Create `tests/artifactSmartEdit.test.mjs`:

```js
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  buildDocSelection,
  composeAiEditPrompt,
  isPreviewLocallyEditable,
  isPreviewStyleEditable,
  supportsPreviewSelection,
} from '../node_modules/.cache/artifact-smart-edit/previewSelection.js';
import { capSelectionPreview, needSwitchConfirm } from '../node_modules/.cache/artifact-smart-edit/docSelection.js';

test('supports selection on text, html, and office kinds', () => {
  for (const kind of ['markdown', 'text', 'code', 'json', 'html', 'docx', 'spreadsheet', 'presentation']) {
    assert.equal(supportsPreviewSelection(kind), true, kind);
  }
  assert.equal(supportsPreviewSelection('pdf'), false);
  assert.equal(supportsPreviewSelection('image'), false);
});

test('style bar only for markdown/text; local edit for text-like kinds', () => {
  assert.equal(isPreviewStyleEditable('markdown'), true);
  assert.equal(isPreviewStyleEditable('html'), false);
  assert.equal(isPreviewLocallyEditable('code'), true);
  assert.equal(isPreviewLocallyEditable('docx'), false);
});

test('buildDocSelection caps preview and labels range', () => {
  const sel = buildDocSelection({
    kind: 'markdown',
    path: 'notes/a.md',
    title: 'a.md',
    selectedText: 'hello world',
  });
  assert.ok(sel);
  assert.equal(sel.kind, 'markdown');
  assert.equal(sel.path, 'notes/a.md');
  assert.equal(sel.preview, 'hello world');
  assert.match(sel.range, /选区|Markdown/);
});

test('composeAiEditPrompt uses @file when path present', () => {
  const sel = buildDocSelection({
    kind: 'text',
    path: 'src/a.py',
    selectedText: 'print(1)',
    range: '文本选区',
  });
  const prompt = composeAiEditPrompt(sel, '改成 print(2)');
  assert.match(prompt, /@file:src\/a\.py/);
  assert.match(prompt, /> print\(1\)/);
  assert.match(prompt, /改成 print\(2\)/);
});

test('composeAiEditPrompt falls back to filename when path empty', () => {
  const sel = buildDocSelection({
    kind: 'docx',
    path: '',
    title: 'report.docx',
    selectedText: '段落',
  });
  const prompt = composeAiEditPrompt(sel, '缩短');
  assert.doesNotMatch(prompt, /@file:/);
  assert.match(prompt, /report\.docx/);
  assert.match(prompt, /> 段落/);
});

test('capSelectionPreview truncates with ellipsis', () => {
  const long = 'x'.repeat(5000);
  const capped = capSelectionPreview(long);
  assert.ok(capped.length < long.length);
  assert.ok(capped.endsWith('…'));
});

test('needSwitchConfirm only across different files', () => {
  const a = buildDocSelection({ kind: 'text', path: 'a.txt', selectedText: 'one' });
  const b = buildDocSelection({ kind: 'text', path: 'a.txt', selectedText: 'two' });
  const c = buildDocSelection({ kind: 'text', path: 'b.txt', selectedText: 'two' });
  assert.equal(needSwitchConfirm(null, a), false);
  assert.equal(needSwitchConfirm(a, b), false);
  assert.equal(needSwitchConfirm(a, c), true);
});
```

- [ ] **Step 2: Add npm script and run test to verify it fails**

In `package.json` scripts add:

```json
"test:artifact-smart-edit": "esbuild src/components/ArtifactsPanel/docSelection.ts src/components/ArtifactsPanel/previewSelection.ts --bundle --packages=external --platform=node --format=esm --outdir=node_modules/.cache/artifact-smart-edit && node --test tests/artifactSmartEdit.test.mjs"
```

Run:

```bash
cd jiuwenswarm/channels/web/frontend && npm run test:artifact-smart-edit
```

Expected: FAIL (modules missing / esbuild cannot resolve).

- [ ] **Step 3: Implement `docSelection.ts`**

```ts
export type SelectionKind = 'doc' | 'slide' | 'sheet' | 'html' | 'markdown' | 'text';

export interface DocSelection {
  kind: SelectionKind;
  source: string;
  path: string;
  range: string;
  preview: string;
}

export const SELECTION_PREVIEW_CAP = 4000;

export function capSelectionPreview(text: string, max = SELECTION_PREVIEW_CAP): string {
  const t = (text || '').trim();
  if (t.length <= max) return t;
  return `${t.slice(0, max)}…`;
}

export function needSwitchConfirm(current: DocSelection | null, next: DocSelection): boolean {
  if (!current) return false;
  const id = (s: DocSelection) => `${s.kind}::${s.path || s.source}`;
  return id(current) !== id(next);
}
```

- [ ] **Step 4: Implement `previewSelection.ts`**

Port gating/build/DOM/position from agent-wb; map WorkSwarm kinds:

- `docx` → `doc`
- `spreadsheet` → `sheet`
- `presentation` → `slide`
- `code` / `json` / `jsonl` → `text` selection kind
- `supportsPreviewSelection`: markdown, text, code, json, jsonl, html, docx, spreadsheet, presentation
- `isPreviewStyleEditable`: markdown | text only
- `isPreviewLocallyEditable`: markdown | text | code | json | jsonl

`composeAiEditPrompt`:

```ts
export function composeAiEditPrompt(sel: DocSelection, instruction: string): string {
  const quote = sel.preview;
  const range = sel.range ? ` [${sel.range}]` : '';
  const header = sel.path.trim()
    ? `@file:${sel.path}${range}`
    : `文件「${sel.source}」${range}`;
  const body = (instruction || '').trim();
  return `${header}\n> ${quote}${body ? `\n\n${body}` : ''}`;
}
```

Include `readDomSelectionText` and `floatingBarPosition` as in agent-wb `previewSelection.ts`.

- [ ] **Step 5: Run tests**

```bash
cd jiuwenswarm/channels/web/frontend && npm run test:artifact-smart-edit
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add jiuwenswarm/channels/web/frontend/src/components/ArtifactsPanel/docSelection.ts \
  jiuwenswarm/channels/web/frontend/src/components/ArtifactsPanel/previewSelection.ts \
  jiuwenswarm/channels/web/frontend/tests/artifactSmartEdit.test.mjs \
  jiuwenswarm/channels/web/frontend/package.json
git commit -m "$(cat <<'EOF'
feat(web): add artifact preview selection model and AI-edit prompt compose

EOF
)"
```

---

### Task 2: Submit-mode preference + General setting

**Files:**
- Create: `jiuwenswarm/channels/web/frontend/src/features/artifactAiEditPreference.ts`
- Modify: `jiuwenswarm/channels/web/frontend/src/features/settings/modules/general/GeneralSettings.tsx`
- Modify: `jiuwenswarm/channels/web/frontend/src/features/settings/modules/general/definition.ts`
- Modify: `jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json`
- Modify: `jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json`
- Modify: `jiuwenswarm/channels/web/frontend/tests/artifactSmartEdit.test.mjs`
- Modify: `jiuwenswarm/channels/web/frontend/package.json` (extend esbuild inputs)

**Interfaces:**
- Consumes: none from Task 1
- Produces:
  - `type ArtifactAiEditSubmitMode = 'auto_send' | 'fill_only'`
  - `ARTIFACT_AI_EDIT_SUBMIT_MODE_KEY = 'jiuwenswarm_artifact_ai_edit_submit_mode'`
  - `readArtifactAiEditSubmitMode(): ArtifactAiEditSubmitMode` (default `auto_send`)
  - `persistArtifactAiEditSubmitMode(mode: ArtifactAiEditSubmitMode): void`

- [ ] **Step 1: Extend tests for preference**

Append to `tests/artifactSmartEdit.test.mjs` (import from cache after bundling preference module):

```js
import {
  persistArtifactAiEditSubmitMode,
  readArtifactAiEditSubmitMode,
} from '../node_modules/.cache/artifact-smart-edit/artifactAiEditPreference.js';

test('artifact AI edit submit mode defaults to auto_send and persists', () => {
  const memory = new Map();
  globalThis.localStorage = {
    getItem: (k) => (memory.has(k) ? memory.get(k) : null),
    setItem: (k, v) => { memory.set(k, String(v)); },
    removeItem: (k) => { memory.delete(k); },
  };
  assert.equal(readArtifactAiEditSubmitMode(), 'auto_send');
  persistArtifactAiEditSubmitMode('fill_only');
  assert.equal(readArtifactAiEditSubmitMode(), 'fill_only');
  persistArtifactAiEditSubmitMode('auto_send');
  assert.equal(readArtifactAiEditSubmitMode(), 'auto_send');
});
```

Update `test:artifact-smart-edit` esbuild entry list to include `src/features/artifactAiEditPreference.ts`.

- [ ] **Step 2: Run test — expect FAIL**

```bash
cd jiuwenswarm/channels/web/frontend && npm run test:artifact-smart-edit
```

- [ ] **Step 3: Implement preference module**

Mirror `workModeStorage.ts` pattern:

```ts
export type ArtifactAiEditSubmitMode = 'auto_send' | 'fill_only';
export const ARTIFACT_AI_EDIT_SUBMIT_MODE_KEY = 'jiuwenswarm_artifact_ai_edit_submit_mode';
const DEFAULT_MODE: ArtifactAiEditSubmitMode = 'auto_send';

export function readArtifactAiEditSubmitMode(): ArtifactAiEditSubmitMode {
  if (typeof window === 'undefined' && typeof globalThis.localStorage === 'undefined') return DEFAULT_MODE;
  try {
    const stored = (globalThis.localStorage ?? window.localStorage).getItem(ARTIFACT_AI_EDIT_SUBMIT_MODE_KEY);
    return stored === 'fill_only' ? 'fill_only' : DEFAULT_MODE;
  } catch {
    return DEFAULT_MODE;
  }
}

export function persistArtifactAiEditSubmitMode(mode: ArtifactAiEditSubmitMode): void {
  try {
    (globalThis.localStorage ?? window.localStorage).setItem(ARTIFACT_AI_EDIT_SUBMIT_MODE_KEY, mode);
  } catch {
    /* ignore */
  }
}
```

- [ ] **Step 4: Add settings UI**

In `GeneralSettings.tsx`, export `ArtifactAiEditSubmitModeSetting` using `SettingRow` + `Select` (same pattern as `DesktopCloseBehaviorSetting`), reading/writing via the preference helpers.

In `definition.ts`, add after connection-status:

```ts
{ id: 'artifact-ai-edit-submit-mode', component: 'custom', render: ArtifactAiEditSubmitModeSetting },
```

i18n keys under `settingsPanel.general`:

- `artifactAiEditSubmitMode`
- `artifactAiEditSubmitModeDescription`
- `artifactAiEditSubmitModeOptions.autoSend`
- `artifactAiEditSubmitModeOptions.fillOnly`

ZH example: 「产物 AI 编辑提交方式」/ 「选择划选 AI 编辑后是自动发送还是仅填入输入框。」/ 「自动发送」/ 「仅填入输入框」.

EN: 「Artifact AI edit submit mode」/ 「Choose whether AI edit from artifact preview auto-sends or only fills the composer.」/ 「Auto-send」/ 「Fill only」.

`data-testid="settings-artifact-ai-edit-submit-mode"`.

- [ ] **Step 5: Run tests**

```bash
cd jiuwenswarm/channels/web/frontend && npm run test:artifact-smart-edit
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add jiuwenswarm/channels/web/frontend/src/features/artifactAiEditPreference.ts \
  jiuwenswarm/channels/web/frontend/src/features/settings/modules/general/GeneralSettings.tsx \
  jiuwenswarm/channels/web/frontend/src/features/settings/modules/general/definition.ts \
  jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json \
  jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json \
  jiuwenswarm/channels/web/frontend/tests/artifactSmartEdit.test.mjs \
  jiuwenswarm/channels/web/frontend/package.json
git commit -m "$(cat <<'EOF'
feat(web): add artifact AI edit submit-mode preference

EOF
)"
```

---

### Task 3: Preview AI-edit bridge

**Files:**
- Create: `jiuwenswarm/channels/web/frontend/src/features/previewAiEditBridge.ts`
- Modify: `tests/artifactSmartEdit.test.mjs`, `package.json` esbuild entries

**Interfaces:**
- Consumes: `readArtifactAiEditSubmitMode()` from Task 2
- Produces:
  - `type PreviewAiEditRequest = { id: string; prompt: string; mode: ArtifactAiEditSubmitMode }`
  - `submitPreviewAiEdit(prompt: string): void` — stamps mode from preference
  - `consumePreviewAiEditRequest(): PreviewAiEditRequest | null`
  - `getPreviewAiEditRequest(): PreviewAiEditRequest | null`
  - `subscribePreviewAiEdit(listener: () => void): () => void`

- [ ] **Step 1: Write failing tests**

```js
import {
  consumePreviewAiEditRequest,
  submitPreviewAiEdit,
  subscribePreviewAiEdit,
} from '../node_modules/.cache/artifact-smart-edit/previewAiEditBridge.js';

test('bridge delivers one-shot request and notifies subscribers', () => {
  let ticks = 0;
  const unsub = subscribePreviewAiEdit(() => { ticks += 1; });
  persistArtifactAiEditSubmitMode('fill_only');
  submitPreviewAiEdit('hello');
  assert.equal(ticks, 1);
  const req = consumePreviewAiEditRequest();
  assert.ok(req);
  assert.equal(req.prompt, 'hello');
  assert.equal(req.mode, 'fill_only');
  assert.equal(consumePreviewAiEditRequest(), null);
  unsub();
});
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement bridge**

```ts
import {
  readArtifactAiEditSubmitMode,
  type ArtifactAiEditSubmitMode,
} from './artifactAiEditPreference';

export type PreviewAiEditRequest = {
  id: string;
  prompt: string;
  mode: ArtifactAiEditSubmitMode;
};

let current: PreviewAiEditRequest | null = null;
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

export function submitPreviewAiEdit(prompt: string): void {
  const text = (prompt || '').trim();
  if (!text) return;
  current = {
    id: `preview-ai-edit-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    prompt: text,
    mode: readArtifactAiEditSubmitMode(),
  };
  notify();
}

export function getPreviewAiEditRequest(): PreviewAiEditRequest | null {
  return current;
}

export function consumePreviewAiEditRequest(): PreviewAiEditRequest | null {
  const req = current;
  current = null;
  return req;
}

export function subscribePreviewAiEdit(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
```

- [ ] **Step 4: Run tests — PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "$(cat <<'EOF'
feat(web): add preview AI-edit request bridge

EOF
)"
```

---

### Task 4: SelectionFloat + text/code/markdown selection (Phase A UI)

**Files:**
- Create: `jiuwenswarm/channels/web/frontend/src/components/ArtifactsPanel/SelectionFloat.tsx`
- Modify: `jiuwenswarm/channels/web/frontend/src/components/ArtifactsPanel/FilePreview.tsx`
- Modify: `jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json`, `en.json` (`artifacts.*` keys)

**Interfaces:**
- Consumes: `buildDocSelection`, `readDomSelectionText`, `floatingBarPosition`, `supportsPreviewSelection`, `composeAiEditPrompt`, `submitPreviewAiEdit`
- Produces: `SelectionFloat` React component used inside preview surface

- [ ] **Step 1: Add i18n keys under `artifacts`**

- `aiEdit`: 「AI 编辑」/ 「AI edit」
- `aiEditPlaceholder`: 「描述如何修改选中内容」/ 「Describe how to change the selection」
- `aiEditSend`: 「发送」/ 「Send」

- [ ] **Step 2: Implement `SelectionFloat.tsx`**

Behavior:

1. Props: `{ kind: PreviewKind; path: string; title: string; panelRef: RefObject<HTMLElement | null>; styleActions?: ... }` (styleActions unused until Task 9; optional).
2. Listen `mouseup` / `keyup` on `panelRef` (capture) → `readDomSelectionText` → if supported kind and selection inside panel, `buildDocSelection` + `floatingBarPosition` → show float (`position: fixed`).
3. Buttons: `data-testid="artifact-ai-edit-btn"` toggles mini form with `artifact-ai-edit-input` + `artifact-ai-edit-send`.
4. On send: `composeAiEditPrompt(sel, instruction)` → `submitPreviewAiEdit(prompt)` → clear selection / close float.
5. Root: `data-testid="artifact-selection-float"`.
6. Use theme tokens only.

Keep component self-contained; no chat store imports besides the bridge.

- [ ] **Step 3: Wire into `FilePreview`**

- Wrap preview content in a relative container with `ref` + `data-testid="artifact-preview-selection-root"`.
- For kinds where `supportsPreviewSelection(kind)` is true **and** the surface is DOM-text (markdown/text/code/json/jsonl/html), mount `<SelectionFloat ... />`.
- For markdown rendered via `MarkdownRenderer`, selection works on rendered DOM text (path may be empty).
- Pass `path: artifact.path ?? ''`, `title: artifact.name`.

- [ ] **Step 4: Manual smoke**

```bash
cd jiuwenswarm/channels/web/frontend && npm run build
```

Expected: typecheck/build succeeds.

Open a session artifact `.md` / `.py`, select text, confirm float appears (full send wired in Task 6).

- [ ] **Step 5: Commit**

```bash
git commit -m "$(cat <<'EOF'
feat(web): add artifact selection float for AI edit

EOF
)"
```

---

### Task 5: Office + HTML selection roots

**Files:**
- Modify: `FilePreview.tsx`
- Modify: `DocxPreview.tsx` / `PresentationPreview.tsx` / `SpreadsheetPreview.tsx` only if needed to expose a selectable text container ref or class

**Interfaces:**
- Consumes: Task 4 `SelectionFloat`
- Produces: selection AI available on html iframe? **HTML:** prefer selecting from sandboxed preview body if same-origin accessible; if iframe blocks selection access, select from a text fallback layer or skip with comment in code — prefer reading selection from the preview host when html is rendered as srcdoc/blob same-origin. If current html preview cannot expose selection, render a read-only text extract sibling for selection (YAGNI: first try `contentDocument` on same-origin iframe; if cross-origin, document limitation in component comment and leave AI edit unavailable for that case only).

- [ ] **Step 1: Inspect current html/docx/pptx/xlsx preview DOM**

Confirm where user-visible text lives. Attach `SelectionFloat` panelRef to the element that contains selectable text (docx HTML root, slide text layer, spreadsheet grid text).

- [ ] **Step 2: Wire SelectionFloat for `html`, `docx`, `spreadsheet`, `presentation`**

Pass optional `range` hints when available (e.g. current slide index → `Slide N`, sheet name → sheet label); otherwise rely on `defaultRangeLabel`.

- [ ] **Step 3: Build**

```bash
cd jiuwenswarm/channels/web/frontend && npm run build
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git commit -m "$(cat <<'EOF'
feat(web): enable AI-edit selection on html and office previews

EOF
)"
```

---

### Task 6: InputArea consumes bridge (Phase A complete)

**Files:**
- Modify: `jiuwenswarm/channels/web/frontend/src/components/ChatPanel/InputArea.tsx`

**Interfaces:**
- Consumes: `subscribePreviewAiEdit`, `consumePreviewAiEditRequest`
- Produces: fill-only via `setInputValue` + contenteditable sync; auto-send via existing `handleSubmit` / `onSubmit` path

- [ ] **Step 1: Add subscription effect in InputArea**

Near other `useEffect` hooks that sync composer state:

```tsx
useEffect(() => {
  return subscribePreviewAiEdit(() => {
    const req = consumePreviewAiEditRequest();
    if (!req) return;
    const sid = useChatStore.getState().activeSessionId;
    if (!sid) return;
    useChatStore.getState().setInputValue(sid, req.prompt);
    if (inputRef.current) {
      // mirror existing plain-text set patterns in this file
      inputRef.current.textContent = req.prompt;
    }
    if (req.mode === 'auto_send') {
      // defer so state/DOM settle before submit
      queueMicrotask(() => {
        handleSubmitRef.current?.();
      });
    }
  });
}, []);
```

Keep a `handleSubmitRef` updated each render (`handleSubmitRef.current = handleSubmit`) to avoid stale closures — follow any existing ref pattern in the file if present.

- [ ] **Step 2: Verify busy-session behavior**

Do not add a side channel. Auto-send must go through the same `handleSubmit` that already queues / interrupts.

- [ ] **Step 3: Build**

```bash
cd jiuwenswarm/channels/web/frontend && npm run build
```

- [ ] **Step 4: Manual checklist**

1. Setting `auto_send`: select text in artifact → AI edit → message sends.
2. Setting `fill_only`: prompt lands in composer, not sent.
3. Artifact without `path`: prompt uses `文件「name」` form.

- [ ] **Step 5: Commit**

```bash
git commit -m "$(cat <<'EOF'
feat(web): wire artifact AI edit into chat InputArea

EOF
)"
```

---

### Task 7: Local text wrap helpers (Phase B)

**Files:**
- Create: `jiuwenswarm/channels/web/frontend/src/components/ArtifactsPanel/previewTextEdit.ts`
- Modify: `tests/artifactSmartEdit.test.mjs`, `package.json` esbuild entries

**Interfaces:**
- Produces: `wrapTextSelection`, `wrapFirstOccurrence`, `TextWrapStyle` (port from agent-wb `previewTextEdit.ts`; omit HTML strip helpers unless FilePreview already needs them)

- [ ] **Step 1: Failing tests**

```js
import { wrapFirstOccurrence, wrapTextSelection } from '../node_modules/.cache/artifact-smart-edit/previewTextEdit.js';

test('wrapTextSelection bold wraps selection', () => {
  const r = wrapTextSelection('hello world', 0, 5, 'bold');
  assert.equal(r.value, '**hello** world');
  assert.equal(r.selectionStart, 2);
  assert.equal(r.selectionEnd, 7);
});

test('wrapFirstOccurrence finds needle', () => {
  const r = wrapFirstOccurrence('aaa bbb aaa', 'bbb', 'italic');
  assert.ok(r);
  assert.equal(r.value, 'aaa _bbb_ aaa');
});
```

- [ ] **Step 2: Run — FAIL; implement port; PASS; commit**

```bash
git commit -m "$(cat <<'EOF'
feat(web): add artifact preview text wrap helpers

EOF
)"
```

---

### Task 8: Editable text previews + save

**Files:**
- Modify: `CodePreview.tsx` — add props `editable?: boolean`, `onChange?: (value: string) => void`
- Modify: `FilePreview.tsx` — edit buffer state for locally editable kinds; save API
- Modify: `ArtifactExpandedPanel.tsx` — pass edit mode / save handlers (toolbar in Task 9 can land here together if smaller; prefer toolbar chrome in Task 9, buffer+save plumbing here)
- Optionally extract: `src/components/ArtifactsPanel/saveArtifactFile.ts` wrapping FileViewer’s POST body

**Interfaces:**
- Consumes: `isPreviewLocallyEditable`, `POST /file-api/file-content` `{ path, content }`
- Produces: `FilePreview` reports `{ dirty, canSave, save(), setEditing() }` via callback props upward **or** controlled props from `ArtifactExpandedPanel`

Recommended controlled API on `FilePreview`:

```ts
type FilePreviewProps = {
  artifact: PreviewArtifact;
  editing?: boolean;
  onDirtyChange?: (dirty: boolean) => void;
  onEditingContentChange?: (content: string) => void;
  saveRequestId?: number; // parent increments to trigger save
  onSaveResult?: (ok: boolean, error?: string) => void;
  // existing props...
};
```

- [ ] **Step 1: Make `CodePreview` honor `editable`**

When `editable`, use `EditorState.readOnly.of(false)` and `EditorView.editable.of(true)`, and `EditorView.updateListener` to call `onChange`.

When not editable, keep current read-only behavior.

- [ ] **Step 2: TextPreview editing**

For markdown/text/code/json/jsonl with `editing && artifact.path`:

- Keep a `draft` string initialized from loaded content.
- markdown: show `CodePreview`/`textarea` in edit mode instead of `MarkdownRenderer`.
- Mark dirty when draft ≠ loaded.
- Save function:

```ts
await fetch('/file-api/file-content', {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ path: artifact.path, content: draft }),
});
```

On success: clear dirty, update baseline content. On failure: keep draft, surface error via `onSaveResult` / alert with `artifacts.saveFailed`.

- [ ] **Step 3: Disable save path when `!artifact.path`**

Even if editing UI somehow enabled, save must no-op / error clearly.

- [ ] **Step 4: i18n**

- `artifacts.edit` / `artifacts.done` / `artifacts.save` / `artifacts.saveFailed` / `artifacts.unsavedConfirm`

- [ ] **Step 5: Build + commit**

```bash
cd jiuwenswarm/channels/web/frontend && npm run build
git commit -m "$(cat <<'EOF'
feat(web): allow local edit and save for text artifact previews

EOF
)"
```

---

### Task 9: Toolbar, dirty confirm, style bar (Phase B complete)

**Files:**
- Modify: `ArtifactExpandedPanel.tsx`
- Modify: `SelectionFloat.tsx` — style buttons when `isPreviewStyleEditable(kind) && editing && path`
- Modify: i18n if any missing keys

**Interfaces:**
- Consumes: Tasks 7–8 helpers and FilePreview controlled API
- Produces: full Phase B UX per spec

- [ ] **Step 1: Toolbar controls**

In `artifact-preview-toolbar`, when selected artifact kind is locally editable **and** has path:

- Toggle `artifact-edit-toggle` (Edit/Done)
- `artifact-save` enabled only when dirty

When no path: do not show Edit/Save (AI selection still works from Phase A).

- [ ] **Step 2: Dirty navigation confirm**

Before `onSelectArtifact('')` (back), prev/next, or selecting another file while dirty: `window.confirm(t('artifacts.unsavedConfirm'))`; cancel aborts navigation.

- [ ] **Step 3: Style bar on float**

When editing markdown/text with path and there is an active selection against the draft:

- Buttons bold/italic/code/link (`artifact-style-bold`, etc.)
- Apply `wrapFirstOccurrence(draft, selectedText, style)` (or precise offsets if textarea/CodeMirror selection available) and update draft.

Do **not** call the AI bridge from style buttons.

- [ ] **Step 4: Build + unit tests still green**

```bash
cd jiuwenswarm/channels/web/frontend && npm run test:artifact-smart-edit && npm run build
```

- [ ] **Step 5: Manual acceptance (spec test plan)**

1. md/code/html/docx/xlsx/pptx: selection AI edit once each.
2. auto_send vs fill_only.
3. no-path artifact: AI works; Edit/Save hidden.
4. md: Edit → bold via style bar → Save → reload preview shows change.
5. dirty switch prompts confirm.

- [ ] **Step 6: Commit**

```bash
git commit -m "$(cat <<'EOF'
feat(web): finish artifact preview local edit toolbar and style bar

EOF
)"
```

- [ ] **Step 7: Mark spec status**

Update `docs/superpowers/specs/2026-10-04-artifact-smart-edit-design.md` status line from `Draft for review` to `Implemented` (or leave if partial — only after both phases done).

---

## Spec coverage checklist

| Spec requirement | Task |
| --- | --- |
| Phase A selection + AI edit | 1, 4, 5, 6 |
| File type matrix (incl. Office) | 1, 4, 5 |
| Prompt with/without path | 1, 6 |
| auto_send / fill_only setting | 2, 3, 6 |
| Bridge → InputArea | 3, 6 |
| Phase B local edit + save | 8, 9 |
| MD/text style bar | 7, 9 |
| Dirty switch confirm | 9 |
| No new backend API / no Office write-back | enforced by non-goals + Task 8 scope |
| i18n + testids | 2, 4, 8, 9 |
| Unit tests for pure logic | 1, 2, 3, 7 |

## Self-review notes

- No TBD placeholders in task steps.
- Types (`DocSelection`, submit mode, bridge request) are consistent across tasks.
- HTML iframe selection is the only environment-dependent edge; Task 5 documents the fallback rule explicitly.
