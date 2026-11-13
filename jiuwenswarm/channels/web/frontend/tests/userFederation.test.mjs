import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

function storage() {
  const entries = new Map();
  return {
    getItem: (key) => entries.get(key) ?? null,
    setItem: (key, value) => entries.set(key, value),
    removeItem: (key) => entries.delete(key),
  };
}

test('portal OIDC callback validates login state and logout clears the OIDC session', async () => {
  const calls = [];
  let user = {
    access_token: 'external-access',
    id_token: 'external-id',
    token_type: 'Bearer',
    state: { connection_id: 'keycloak' },
  };
  globalThis.localStorage = storage();
  globalThis.sessionStorage = storage();
  globalThis.window = {
    sessionStorage,
    location: {
      origin: 'https://example.test',
      pathname: '/portal/auth',
      href: 'https://example.test/portal/auth',
    },
  };
  window.parent = window;
  globalThis.fetch = async () => Response.json({
    connection_id: 'keycloak',
    flow: 'browser_pkce',
    authority: 'https://idp.example.test/realms/test',
    client_id: 'user-web',
    scope: 'openid',
    automatic_silent_renew: true,
  });
  globalThis.testUserManager = class {
    constructor(options) { calls.push(['configure', options]); }
    async clearStaleState() {}
    async signinRedirect(options) { calls.push(['signin', options]); }
    async signinCallback(url) { calls.push(['callback', url]); return user; }
    async getUser() { return user; }
    async removeUser() { calls.push(['removeUser']); }
    async signinSilentCallback(url) { calls.push(['silent', url]); }
    async signoutRedirect(options) { calls.push(['signout', options]); }
    async signoutCallback(url) { calls.push(['signoutCallback', url]); }
  };

  const { outputFiles } = await build({
    entryPoints: ['src/auth/manager/federation.ts'],
    bundle: true,
    format: 'esm',
    platform: 'node',
    write: false,
    define: { 'import.meta.env': JSON.stringify({ BASE_URL: '/portal/' }) },
    plugins: [{
      name: 'mock-oidc-client',
      setup(plugin) {
        plugin.onResolve({ filter: /^oidc-client-ts$/ }, () => ({ path: 'oidc-client-ts', namespace: 'test' }));
        plugin.onLoad({ filter: /.*/, namespace: 'test' }, () => ({
          contents: 'export const UserManager = globalThis.testUserManager; export class WebStorageStateStore {}',
        }));
      },
    }],
  });
  const federation = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].contents).toString('base64')}`);
  await assert.rejects(federation.consumeBrowserCallback('keycloak'), /Missing login context/);
  await federation.beginBrowserLogin('keycloak');
  const options = calls[0][1];
  for (const [key, suffix] of Object.entries({
    redirect_uri: 'callback',
    silent_redirect_uri: 'silent-callback',
    post_logout_redirect_uri: 'logout-callback',
  })) {
    assert.equal(options[key], `https://example.test/portal/auth/federation/keycloak/${suffix}`);
  }
  assert.deepEqual(calls[1], ['signin', { state: { connection_id: 'keycloak' } }]);
  window.location.href = options.redirect_uri;
  user.state.connection_id = 'wrong';
  await assert.rejects(federation.consumeBrowserCallback('keycloak'), /Invalid OIDC login result/);
  assert.deepEqual(calls.at(-1), ['removeUser']);
  await assert.rejects(federation.consumeBrowserCallback('keycloak'), /Missing login context/);

  await federation.beginBrowserLogin('keycloak');
  user.state.connection_id = 'keycloak';
  assert.equal(await federation.consumeBrowserCallback('keycloak'), 'external-access');
  await assert.rejects(federation.consumeSilentCallback('keycloak'), /requires an iframe/);
  window.parent = {};
  window.location.href = options.silent_redirect_uri;
  await federation.consumeSilentCallback('keycloak');
  assert.deepEqual(calls.at(-1), ['silent', options.silent_redirect_uri]);

  federation.rememberBrowserSession('keycloak');
  assert.equal(localStorage.getItem('openjiuwen_oidc_renew'), 'true');
  assert.equal(await federation.endBrowserSession(), true);
  assert.deepEqual(calls.at(-1), ['signout', { id_token_hint: 'external-id' }]);
  window.location.href = options.post_logout_redirect_uri;
  await federation.consumeLogoutCallback('keycloak');
  assert.deepEqual(calls.slice(-2), [['signoutCallback', options.post_logout_redirect_uri], ['removeUser']]);
  assert.equal(localStorage.getItem('openjiuwen_oidc_connection'), null);
  assert.equal(localStorage.getItem('openjiuwen_oidc_renew'), null);
});
