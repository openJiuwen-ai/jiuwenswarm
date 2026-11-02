import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const apiSource = readFileSync(
  new URL('../src/services/skillUploadPermissionApi.ts', import.meta.url),
  'utf8',
);
const widgetsSource = readFileSync(
  new URL('../src/components/SkillPanel/SkillPanelWidgets.tsx', import.meta.url),
  'utf8',
);
const panelSource = readFileSync(
  new URL('../src/components/SkillPanel/index.tsx', import.meta.url),
  'utf8',
);
const zhSource = readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8');
const enSource = readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url), 'utf8');

test('cancelSkillUploadRequest targets the user-console cancel endpoint with POST', () => {
  assert.match(
    apiSource,
    /export async function cancelSkillUploadRequest\(orderNum: string\)/,
  );
  assert.match(
    apiSource,
    /`\/manager-api\/v1\/user-console\/approvals\/\$\{encodeURIComponent\(orderNum\)\}\/cancel`/,
  );
  assert.match(apiSource, /method:\s*'POST'/);
});

test('SkillUploadPermissionEntry exposes a withdraw button in the pending phase', () => {
  assert.match(widgetsSource, /onCancel:\s*\(\)\s*=>\s*void/);
  assert.match(widgetsSource, /cancelling\s*=\s*false/);
  assert.match(widgetsSource, /state\.phase === 'pending'/);
  assert.match(widgetsSource, /data-testid="skill-panel-create-permission-cancel-btn"/);
  assert.match(widgetsSource, /skills\.skillUpload\.entryCancel/);
  assert.match(widgetsSource, /skills\.skillUpload\.entryCancelling/);
  // pending 状态下入口不再 disabled，撤回按钮可点
  const pendingBlock = widgetsSource.slice(
    widgetsSource.indexOf("if (state.phase === 'pending')"),
    widgetsSource.indexOf('const disabled = state.phase ==='),
  );
  assert.match(pendingBlock, /onClick=\{onCancel\}/);
});

test('SkillPanel wires the cancel handler to the entry and refreshes permission after cancel', () => {
  assert.match(panelSource, /import \{ cancelSkillUploadRequest,/);
  assert.match(panelSource, /handleCancelSkillUploadPermission = useCallback/);
  assert.match(
    panelSource,
    /await cancelSkillUploadRequest\(orderNum\);/,
  );
  assert.match(panelSource, /window\.confirm\(t\('skills\.skillUpload\.cancelConfirm'\)\)/);
  assert.match(panelSource, /skills\.skillUpload\.cancelSuccess/);
  assert.match(panelSource, /skills\.skillUpload\.cancelFailed/);
  assert.match(panelSource, /await refreshSkillUploadPermission\(\)/);
  assert.match(
    panelSource,
    /onCancel=\{\(\) => void handleCancelSkillUploadPermission\(\)\}/,
  );
  assert.match(panelSource, /cancelling=\{cancellingUploadPermission\}/);
});

test('cancel-related i18n keys exist in both zh and en locales', () => {
  for (const source of [zhSource, enSource]) {
    assert.match(source, /"entryCancel"/);
    assert.match(source, /"entryCancelling"/);
    assert.match(source, /"cancelConfirm"/);
    assert.match(source, /"cancelSuccess"/);
    assert.match(source, /"cancelFailed"/);
  }
});
