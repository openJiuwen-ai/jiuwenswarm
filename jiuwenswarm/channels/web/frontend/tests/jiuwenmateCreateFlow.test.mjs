import assert from 'node:assert/strict';
import test from 'node:test';

import {
  cancelColleagueCreation,
  closeTemplateDetail,
  completeColleagueCreation,
  createColleagueId,
  createInitialJiuwenMateState,
  deleteColleague,
  finishColleagueAuthorization,
  openColleagueAuthorization,
  openColleagueCreation,
  openColleagueMessages,
  openCustomColleagueCreation,
  officeActivityLoad,
  openOffice,
  replaceColleagues,
  selectColleagueTemplate,
  setColleagueDisabled,
  startColleagueRefinement,
  updateColleagueDraft,
} from '../node_modules/.cache/jiuwenmate-create-flow/createFlowState.js';

const draft = {
  name: '品牌顾问',
  description: '提供品牌定位与传播建议。',
  tags: ['效率提升'],
  category: 'custom',
  capabilities: { model: 'deepseek-v3.2', skills: [], connectors: [], plugins: [], knowledge: [], starterPrompts: [] },
};

const colleague = { ...draft, id: 'brand-advisor', imConfigured: false, disabled: false, joinedAt: '2026-09-02T10:09:00+08:00' };

test('template detail opens and closes without leaving the selection step', () => {
  const selecting = openColleagueCreation(createInitialJiuwenMateState([]));
  assert.equal(selecting.view, 'colleague-create');
  assert.equal(selecting.creationStep, 'select');

  const detail = selectColleagueTemplate(selecting, 'system-architect');
  assert.equal(detail.detailTemplateId, 'system-architect');
  assert.equal(closeTemplateDetail(detail).detailTemplateId, null);

  const cancelled = cancelColleagueCreation(detail);
  assert.equal(cancelled.view, 'office-intro');
  assert.equal(cancelled.detailTemplateId, null);
});

test('custom creation opens the dialog and refinement keeps the draft editable', () => {
  const dialog = openCustomColleagueCreation(createInitialJiuwenMateState([]));
  assert.equal(dialog.customDialogOpen, true);

  const refining = startColleagueRefinement(dialog, 'custom', draft);
  assert.equal(refining.creationStep, 'refine');
  assert.equal(refining.creationSource, 'custom');
  assert.equal(refining.customDialogOpen, false);
  const renamed = updateColleagueDraft(refining, { name: '售后群助理' });
  assert.equal(renamed.draft.name, '售后群助理');

  const tinted = startColleagueRefinement(dialog, 'custom', { ...draft, avatarHue: 110 });
  assert.equal(updateColleagueDraft(tinted, { name: '售后群助理' }).draft.avatarHue, 110);
});

test('completing creation opens the profile with the IM authorization prompt once', () => {
  const refining = startColleagueRefinement(openColleagueCreation(createInitialJiuwenMateState([])), 'custom', draft);
  const created = completeColleagueCreation(refining, colleague);

  assert.equal(created.view, 'colleague-profile');
  assert.equal(created.selectedColleagueId, 'brand-advisor');
  assert.equal(created.authorizeColleagueId, 'brand-advisor');
  assert.deepEqual(created.colleagues.map((item) => item.id), ['brand-advisor']);

  const skipped = finishColleagueAuthorization(created, false);
  assert.equal(skipped.authorizeColleagueId, null);
  assert.equal(skipped.colleagues[0].imConfigured, false);

  const configured = finishColleagueAuthorization(openColleagueAuthorization(skipped, 'brand-advisor'), true);
  assert.equal(configured.colleagues[0].imConfigured, true);

  const repeated = completeColleagueCreation(configured, colleague);
  assert.deepEqual(repeated.colleagues.map((item) => item.id), ['brand-advisor']);
});

test('office and messages entries depend on whether colleagues exist', () => {
  const empty = createInitialJiuwenMateState([]);
  assert.equal(openOffice(empty).view, 'office-intro');
  assert.equal(openColleagueMessages(empty).view, 'colleague-empty');

  const created = completeColleagueCreation(empty, colleague);
  assert.equal(openOffice(created).view, 'office-dashboard');
  assert.equal(openColleagueMessages(created).view, 'colleague-workspace');
});

test('disable and delete update the colleague collection', () => {
  const created = completeColleagueCreation(createInitialJiuwenMateState([]), colleague);
  assert.equal(setColleagueDisabled(created, 'brand-advisor', true).colleagues[0].disabled, true);

  const deleted = deleteColleague({ ...created, view: 'office-dashboard' }, 'brand-advisor');
  assert.equal(deleted.colleagues.length, 0);
  assert.equal(deleted.view, 'office-intro');
  assert.equal(deleted.selectedColleagueId, null);
});

test('colleague ids stay readable and unique', () => {
  assert.equal(createColleagueId('Brand Advisor', []), 'brand-advisor');
  assert.equal(createColleagueId('品牌顾问', ['colleague']), 'colleague-2');
});

test('office activity reloads when the dashboard is opened again', () => {
  assert.equal(officeActivityLoad('office-dashboard', 'loading'), 'initial');
  assert.equal(officeActivityLoad('colleague-workspace', 'failed'), 'initial');
  assert.equal(officeActivityLoad('office-dashboard', 'ready'), 'refresh');
  assert.equal(officeActivityLoad('colleague-profile', 'ready'), 'refresh');
  assert.equal(officeActivityLoad('colleague-workspace', 'ready'), 'idle');
});

test('server colleagues replace the list and keep page-local IM and disabled marks', () => {
  const xiaojiu = { ...colleague, id: 'xiaojiu', name: '小九', isDefault: true };
  const loaded = replaceColleagues(createInitialJiuwenMateState([]), [xiaojiu]);
  assert.equal(loaded.view, 'office-dashboard');
  assert.equal(loaded.selectedColleagueId, 'xiaojiu');

  const marked = setColleagueDisabled(finishColleagueAuthorization(openColleagueAuthorization(loaded, 'xiaojiu'), true), 'xiaojiu', true);
  const reloaded = replaceColleagues(marked, [{ ...xiaojiu, name: '小九 2' }, colleague]);
  assert.deepEqual(
    reloaded.colleagues.map((item) => [item.id, item.name, item.imConfigured, item.disabled]),
    [
      ['xiaojiu', '小九 2', true, true],
      ['brand-advisor', colleague.name, false, false],
    ],
  );
  assert.equal(reloaded.selectedColleagueId, 'xiaojiu');
  assert.equal(replaceColleagues(reloaded, []).view, 'office-intro');
});
