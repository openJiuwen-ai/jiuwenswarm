import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import { build } from 'esbuild';
await build({
  stdin: {
    contents: `export { AgentAvatar } from './src/components/AgentAvatar';
      export { useAgentCatalogStore } from './src/stores/agentCatalogStore';`,
    resolveDir: process.cwd(), loader: 'tsx',
  },
  bundle: true, packages: 'external', platform: 'node', format: 'esm',
  outfile: 'node_modules/.cache/agent-avatar/identity-regression.mjs',
  loader: { '.svg': 'dataurl', '.png': 'dataurl' },
  define: { 'import.meta.env.DEV': 'false' },
});
const { AgentAvatar, useAgentCatalogStore } = await import(
  '../node_modules/.cache/agent-avatar/identity-regression.mjs'
);
// SSR reads the store's initial snapshot; expose the seeded fixture for each render.
function seedCatalog(state) {
  Object.assign(useAgentCatalogStore.getInitialState(), state);
  useAgentCatalogStore.setState(state);
}
test.afterEach(() => seedCatalog({ catalog: null, status: 'idle' }));

test('selected Agent avatar can render its identity name', () => {
  const markup = renderToStaticMarkup(
    createElement(AgentAvatar, {
      agentId: 'expert-a',
      alt: '',
      showName: true,
    }),
  );

  assert.match(markup, /chat-avatar-name/);
  assert.match(markup, />expert-a<\/span>/);
});

test('Team leader override keeps the frozen display name and fallback avatar path', () => {
  const markup = renderToStaticMarkup(
    createElement(AgentAvatar, {
      identityOverride: {
        agentTemplateId: 'leader-template',
        displayName: '专家团负责人',
      },
      alt: '',
      showName: true,
    }),
  );

  assert.match(markup, /chat-avatar-name/);
  assert.match(markup, />专家团负责人<\/span>/);
});

for (const displayName of ['英语表达教练', 'English Expression Coach']) {
  test(`runtime package name resolves catalog identity: ${displayName}`, () => {
    seedCatalog({ catalog: [{
      id: 'hub-asset-5739', runtimePackageName: 'english-coach', displayName,
      avatarUrl: 'https://example.test/coach.png',
    }], status: 'success' });
    const markup = renderToStaticMarkup(createElement(AgentAvatar, {
      agentId: 'english-coach', showName: true,
    }));
    assert.ok(markup.includes(`>${displayName}</span>`));
    assert.ok(markup.includes('https://example.test/coach.png'));
  });
}

test('exact asset ID takes precedence over another asset runtime name', () => {
  seedCatalog({ catalog: [
    { id: 'other', runtimePackageName: 'expert-a', displayName: 'Wrong expert' },
    { id: 'expert-a', runtimePackageName: 'runtime-a', displayName: 'Correct expert' },
  ], status: 'success' });
  const markup = renderToStaticMarkup(createElement(AgentAvatar, {
    agentId: 'expert-a', showName: true,
  }));
  assert.ok(markup.includes('>Correct expert</span>'));
  assert.ok(!markup.includes('Wrong expert'));
});
