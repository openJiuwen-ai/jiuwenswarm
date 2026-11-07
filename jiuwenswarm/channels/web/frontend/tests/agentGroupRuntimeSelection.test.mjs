import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { transformSync } from 'esbuild';

const source = readFileSync(new URL('../src/components/AgentManagementPanel/index.tsx', import.meta.url), 'utf8');
const guardStart = source.indexOf('  const getSelectableGroup =');
const start = guardStart >= 0 ? guardStart : source.indexOf('  const handleUseGroup =');
const end = source.indexOf('  const handleInstallGroup =', start);

function setup(request) {
  const effects = [];
  const item = { id: 'team-id', name: 'team-name', installed: true, capabilities: { canUse: true } };
  const code = transformSync(source.slice(start, end), { loader: 'ts' }).code;
  const create = new Function(
    'groupCatalogRef', 'groupMineRef', 'webRequest', 'setGroupRuntimeNotice', 't',
    'seedSelectedAgentGroup', 'onUseAgentGroup', 'onUseGroupPrompt',
    `${code}; return { handleUseGroup, handleUseGroupPrompt };`,
  );
  const handlers = create(
    { current: [item] }, { current: [] }, request,
    message => effects.push(['notice', message]),
    (key, options) => options?.agents ?? key,
    selected => effects.push(['seed', selected.name]),
    name => effects.push(['use', name]),
    (name, prompt) => effects.push(['prompt', name, prompt]),
  );
  return { handlers, effects, item };
}

for (const entry of ['handleUseGroup', 'handleUseGroupPrompt']) {
  test(`${entry} blocks disabled runtimes and rechecks after enabling`, async () => {
    let disabled = ['claude', 'codex'];
    const calls = [];
    const { handlers, effects } = setup(async (method, params) => {
      calls.push([method, params]);
      return { group: { disabledRuntimes: disabled } };
    });
    await handlers[entry]('team-id', 'hello');
    assert.deepEqual(effects, [['notice', 'Claude, Codex']]);
    disabled = [];
    effects.length = 0;
    await handlers[entry]('team-id', 'hello');
    assert.deepEqual(effects, [
      ['seed', 'team-name'],
      entry === 'handleUseGroup' ? ['use', 'team-name'] : ['prompt', 'team-name', 'hello'],
    ]);
    assert.deepEqual(calls, Array(2).fill(['agent_groups.show', { id: 'team-id' }]));
  });

  test(`${entry} blocks request failures`, async () => {
    const { handlers, effects } = setup(async () => { throw new Error('offline'); });
    await handlers[entry]('team-id', 'hello');
    assert.deepEqual(effects, [['notice', 'Error: offline']]);
  });

  test(`${entry} preserves installed and usable guards`, async () => {
    const { handlers, effects, item } = setup(async () => { throw new Error('must not request'); });
    item.installed = false;
    await handlers[entry]('team-id', 'hello');
    item.installed = true;
    item.capabilities.canUse = false;
    await handlers[entry]('team-id', 'hello');
    await handlers[entry]('missing', 'hello');
    assert.deepEqual(effects, []);
  });
}
