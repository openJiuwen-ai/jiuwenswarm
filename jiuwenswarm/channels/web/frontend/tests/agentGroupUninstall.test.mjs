import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../src/components/AgentManagementPanel/index.tsx', import.meta.url), 'utf8');
const handler = source.slice(source.indexOf('  const handleUninstallGroup ='), source.indexOf('  const handleReconnect ='))
  .replace('(id: string)', '(id)');

async function uninstall({ source = 'hub', origin = 'mine', view = 'group-detail', confirmed = true, fails = false } = {}) {
  const calls = [];
  const context = {
    groupDetail: { id: 'delisted', source, displayName: 'Test group' },
    groupCatalogRef: { current: [] }, groupMineRef: { current: [] },
    groupSelectedId: 'delisted', groupDetailOrigin: origin, view,
    window: { confirm: () => confirmed }, t: key => key,
    groupClient: { uninstallGroup: async id => { calls.push(['uninstall', id]); if (fails) throw new Error('failed'); return {}; } },
    loadGroups: async scope => calls.push(['load', scope]),
    openGroupDetail: async id => calls.push(['detail', id]),
    formatActionError: error => error.message,
  };
  for (const name of ['markBusy', 'clearBusy', 'setActionNotice', 'setActionNoticeError', 'showActionError', 'setView', 'setMineKind', 'setGroupSelectedId', 'setGroupDetail']) {
    context[name] = value => calls.push([name, value]);
  }
  await vm.runInNewContext(`${handler}\nhandleUninstallGroup('delisted')`, context);
  return calls;
}

for (const source of ['hub', 'local']) {
  for (const origin of ['mine', 'teams']) {
    test(`${source} uninstall returns to ${origin} without requesting deleted detail`, async () => {
      const calls = await uninstall({ source, origin });
      assert.ok(calls.some(([name, value]) => name === 'setView' && value === origin));
      assert.equal(calls.some(([name]) => name === 'detail'), false);
      assert.deepEqual(calls.filter(([name]) => name === 'load'), [['load', 'catalog'], ['load', 'mine']]);
    });
  }
}
test('failed uninstall retains detail and reports error', async () => {
  const calls = await uninstall({ fails: true });
  assert.ok(calls.some(([name]) => name === 'showActionError'));
  assert.equal(calls.some(([name]) => name === 'setView' || name === 'load'), false);
  assert.ok(calls.some(([name]) => name === 'clearBusy'));
});
test('cancelled uninstall does nothing', async () => assert.deepEqual(await uninstall({ confirmed: false }), []));
test('list uninstall refreshes lists without navigating', async () => {
  const calls = await uninstall({ view: 'mine' });
  assert.equal(calls.some(([name]) => name === 'setView' || name === 'detail'), false);
});
