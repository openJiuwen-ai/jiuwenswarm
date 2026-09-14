import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

await build({
  entryPoints: ['src/services/personalContextApi.ts'],
  outfile: 'node_modules/.cache/personal-context-authorization/personalContextApi.mjs',
  bundle: true,
  platform: 'node',
  format: 'esm',
  plugins: [
    {
      name: 'personal-context-web-client',
      setup(builder) {
        builder.onResolve({ filter: /^\.\/webClient$/ }, () => ({
          path: 'webClient',
          namespace: 'mock',
        }));
        builder.onLoad({ filter: /.*/, namespace: 'mock' }, () => ({
          contents: `
            export const webRequest = async (method, params) => {
              globalThis.personalContextRequests.push({ method, params });
              return {
                provider: params.provider,
                state: 'authorized',
                verification_url: null,
                expires_at: null,
                error: null,
              };
            };
            export const webClient = {
              on: () => () => {},
              sendFireAndForget: () => {},
            };
          `,
        }));
      },
    },
  ],
});

const { pcApi } = await import(
  '../node_modules/.cache/personal-context-authorization/personalContextApi.mjs'
);

test('provider reauthorization sends the explicit boolean without changing credential shape', async () => {
  globalThis.personalContextRequests = [];
  await pcApi.authorizeProvider('github', { token: 'token-canary' }, true);
  assert.deepEqual(globalThis.personalContextRequests, [
    {
      method: 'personal_context.fetch.authorize_provider',
      params: {
        provider: 'github',
        credentials: { token: 'token-canary' },
        reauthorize: true,
      },
    },
  ]);
});
