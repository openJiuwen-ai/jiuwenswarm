import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

async function loadPaths(env) {
  const { outputFiles } = await build({
    entryPoints: ['src/auth/manager/userWebPaths.ts'],
    bundle: true,
    format: 'esm',
    platform: 'node',
    write: false,
    define: { 'import.meta.env': JSON.stringify(env) },
  });
  return import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].contents).toString('base64')}`);
}

test('user login and workspace routes share the dedicated origin root', async () => {
  const { userWebPaths, isUserLoginRoute } = await loadPaths({});
  for (const path of ['/', '/index.html', '/chat/session', '/auth', '/settings']) {
    assert.deepEqual(userWebPaths(path), { root: '/', login: '/auth', route: path });
  }
  assert.equal(isUserLoginRoute(userWebPaths('/auth/federation/keycloak/callback').route), true);
  assert.equal(isUserLoginRoute(userWebPaths('/chat/session').route), false);
  assert.equal(userWebPaths('/settings').login, '/auth');
  assert.equal(userWebPaths('/auth').root, '/');
  assert.equal(userWebPaths('//example.test/auth').login, '/auth');
});

test('legacy page and API variables do not change the login origin', async () => {
  const { userWebPaths } = await loadPaths({ BASE_URL: '/portal/', VITE_API_PREFIX: '/proxy' });
  assert.deepEqual(userWebPaths('/portal/settings'), {
    root: '/', login: '/auth', route: '/portal/settings',
  });
  assert.equal(userWebPaths('/auth').root, '/');
  const apiPrefixed = await loadPaths({ VITE_API_PREFIX: '/portal' });
  assert.equal(apiPrefixed.userWebPaths('/portal/settings').login, '/auth');
});
