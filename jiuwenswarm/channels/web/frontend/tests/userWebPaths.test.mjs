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

test('legacy portal paths retain the login and workspace destinations', async () => {
  const { userWebPaths, isUserLoginRoute } = await loadPaths({});
  for (const path of ['/portal/', '/portal/index.html', '/portal/chat/session', '/portal/auth']) {
    assert.equal(userWebPaths(path).root, '/portal/');
    assert.equal(userWebPaths(path).login, '/portal/auth');
  }
  assert.equal(isUserLoginRoute(userWebPaths('/portal/auth/federation/keycloak/callback').route), true);
  assert.equal(isUserLoginRoute(userWebPaths('/portal/chat/session').route), false);
  assert.equal(userWebPaths('/settings').login, '/auth');
  assert.equal(userWebPaths('/auth').root, '/');
  assert.equal(userWebPaths('//example.test/auth').login, '/auth');
});

test('configured portal prefix applies to other SPA routes without confusing an API-only prefix', async () => {
  const { userWebPaths } = await loadPaths({ BASE_URL: '/portal/', VITE_API_PREFIX: '/proxy' });
  assert.deepEqual(userWebPaths('/portal/settings'), {
    root: '/portal/', login: '/portal/auth', route: '/settings',
  });
  assert.equal(userWebPaths('/auth').root, '/');
  const apiPrefixed = await loadPaths({ VITE_API_PREFIX: '/portal' });
  assert.equal(apiPrefixed.userWebPaths('/portal/settings').login, '/portal/auth');
});
