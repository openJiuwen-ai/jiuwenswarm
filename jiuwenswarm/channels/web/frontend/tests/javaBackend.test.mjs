import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { resolveConfig } from 'vite';
import { parseModelConfigurationJson } from '../node_modules/.cache/java-backend/modelConfigImport.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));

test('Java mode proxies files and WebSockets, Python retains its middleware', async (t) => {
  const names = ['JIUWENSWARM_BACKEND', 'WEB_PORT', 'FRONTEND_PORT'];
  const saved = Object.fromEntries(names.map((name) => [name, process.env[name]]));
  t.after(() => {
    for (const name of names) {
      if (saved[name] === undefined) delete process.env[name];
      else process.env[name] = saved[name];
    }
  });
  process.env.JIUWENSWARM_BACKEND = 'java';
  process.env.WEB_PORT = '18999';
  process.env.FRONTEND_PORT = '5174';
  const java = await resolveConfig({ root }, 'serve');
  assert.equal(java.server.host, '127.0.0.1');
  assert.equal(java.server.port, 5174);
  for (const route of ['/api', '/ws', '/ws/git', '/file-api']) {
    assert.equal(java.server.proxy[route].target, 'http://127.0.0.1:18999');
  }
  assert.equal(java.server.proxy['/ws/git'].ws, true);
  delete process.env.JIUWENSWARM_BACKEND;
  delete process.env.WEB_PORT;
  delete process.env.FRONTEND_PORT;
  const python = await resolveConfig({ root }, 'serve');
  assert.equal(python.server.port, 5173);
  assert.equal(python.server.proxy['/file-api'], undefined);
  const extraPlugins = python.plugins.filter((plugin) => !java.plugins.some((other) => other.name === plugin.name));
  assert.equal(extraPlugins.length, 2);
  assert.equal(java.define['import.meta.env.VITE_JIUWENSWARM_BACKEND'], '"java"');
  assert.equal(python.define['import.meta.env.VITE_JIUWENSWARM_BACKEND'], '"python"');
});

test('preflight skips generation only for Java; Python keeps generation', (t) => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'java preflight '));
  t.after(() => fs.rmSync(temporary, { recursive: true, force: true }));
  const entry = path.join(temporary, 'jiuwenswarm/channels/web/frontend/scripts/backend-preflight.cjs');
  const generator = path.join(temporary, 'jiuwenswarm/scripts/generate-agent-folders.js');
  fs.mkdirSync(path.dirname(entry), { recursive: true });
  fs.mkdirSync(path.dirname(generator), { recursive: true });
  fs.copyFileSync(path.join(root, 'scripts/backend-preflight.cjs'), entry);
  fs.writeFileSync(generator, "require('node:fs').writeFileSync('generated', 'fixture');");
  execFileSync(process.execPath, [entry], { env: { ...process.env, JIUWENSWARM_BACKEND: 'java' } });
  assert.equal(fs.existsSync(path.join(temporary, 'generated')), false);
  execFileSync(process.execPath, [entry], { env: { ...process.env, JIUWENSWARM_BACKEND: 'python' } });
  assert.equal(fs.readFileSync(path.join(temporary, 'generated'), 'utf8'), 'fixture');
});

test('model import accepts bootstrap and Python models.defaults formats', () => {
  const bootstrap = parseModelConfigurationJson(
    JSON.stringify({
      MODEL_NAME: 'fixture',
      API_BASE: 'http://127.0.0.1',
      API_KEY: 'fixture-only',
      MODEL_PROVIDER: 'OpenAI',
    }),
  );
  assert.equal(bootstrap[0].model_name, 'fixture');
  assert.equal(bootstrap[0].is_default, true);
  const models = parseModelConfigurationJson(
    JSON.stringify({
      models: {
        defaults: [
          {
            model_client_config: { model_name: 'fixture', api_base: 'http://127.0.0.1', client_provider: 'OpenAI' },
          },
        ],
      },
    }),
  );
  assert.equal(models[0].model_provider, 'OpenAI');
  assert.throws(() => parseModelConfigurationJson('{}'), /requires/);
  assert.throws(() => parseModelConfigurationJson('[]'), /does not contain/);
});
