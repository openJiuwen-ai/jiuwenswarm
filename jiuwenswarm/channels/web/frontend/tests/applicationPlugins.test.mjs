import assert from 'node:assert/strict';
import test from 'node:test';

import { enabledApplicationPlugins, normalizeApplicationPluginManifest } from '../node_modules/.cache/application-plugins/manifest.js';

test('normalizes and orders application plugin contributions', () => {
  const plugins = normalizeApplicationPluginManifest({
    api_version: 1,
    plugins: [
      {
        plugin_id: 'later',
        plugin_version: '1.0.0',
        enabled: true,
        id: 'later-page',
        nav_key: 'app:later',
        title: 'Later',
        render_mode: 'iframe',
        position: 200,
      },
      {
        plugin_id: 'earlier',
        plugin_version: '1.0.0',
        enabled: true,
        id: 'earlier-page',
        nav_key: 'app:earlier',
        title: 'Earlier',
        render_mode: 'bundled',
        position: 75,
      },
    ],
  });

  assert.deepEqual(
    plugins.map(plugin => plugin.plugin_id),
    ['earlier', 'later'],
  );
  assert.equal(plugins[0].enabled, true);
});

test('rejects unsupported and malformed manifests', () => {
  assert.deepEqual(normalizeApplicationPluginManifest({ api_version: 2, plugins: [] }), []);
  assert.deepEqual(
    normalizeApplicationPluginManifest({
      api_version: 1,
      plugins: [{ plugin_id: 'missing-fields' }],
    }),
    [],
  );
});

test('hides disabled plugins from workspace navigation', () => {
  const plugins = normalizeApplicationPluginManifest({
    api_version: 1,
    plugins: [
      {
        plugin_id: 'disabled',
        plugin_version: '1.0.0',
        enabled: false,
        id: 'disabled-page',
        nav_key: 'app:disabled',
        title: 'Disabled',
        render_mode: 'bundled',
        position: 75,
      },
    ],
  });

  assert.equal(plugins.length, 1);
  assert.deepEqual(enabledApplicationPlugins(plugins), []);
});

test('does not add backend-only plugins to navigation', () => {
  const plugins = normalizeApplicationPluginManifest({
    api_version: 1,
    plugins: [
      {
        plugin_id: 'backend-only',
        plugin_version: '1.0.0',
        enabled: true,
        id: 'backend-only:management',
        nav_key: 'app:backend-only',
        title: 'Backend only',
        render_mode: 'none',
        position: 1000,
      },
    ],
  });

  assert.equal(plugins.length, 1);
  assert.deepEqual(enabledApplicationPlugins(plugins), []);
});
