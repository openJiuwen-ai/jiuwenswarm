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

test('carries the identity a plugin declares for its card', () => {
  const [plugin] = normalizeApplicationPluginManifest({
    api_version: 1,
    plugins: [
      {
        plugin_id: 'co-scribe',
        plugin_version: '1.0.0',
        enabled: true,
        id: 'co-scribe-docs',
        nav_key: 'app:co-scribe',
        title: 'Docs',
        title_i18n_key: 'nav.docs',
        description: 'Co-edit cloud documents.',
        description_i18n_key: 'docs.plugin.summary',
        icon: 'data:image/png;base64,iVBORw0KGgo=',
        render_mode: 'bundled',
        position: 80,
      },
    ],
  });

  assert.equal(plugin.icon, 'data:image/png;base64,iVBORw0KGgo=');
  assert.equal(plugin.description_i18n_key, 'docs.plugin.summary');
  assert.equal(plugin.description, 'Co-edit cloud documents.');
});

test('keeps a plugin that declares no identity assets', () => {
  const [plugin] = normalizeApplicationPluginManifest({
    api_version: 1,
    plugins: [
      {
        plugin_id: 'video-duplex',
        plugin_version: '1.0.0',
        enabled: true,
        id: 'video-duplex-live',
        nav_key: 'app:video-duplex',
        title: 'Video',
        render_mode: 'bundled',
        position: 75,
      },
    ],
  });

  assert.equal(plugin.icon, undefined);
  assert.equal(plugin.description_i18n_key, undefined);
});

test('drops an entry whose identity fields are the wrong type', () => {
  // A bad icon must never reach an <img src>.
  assert.deepEqual(
    normalizeApplicationPluginManifest({
      api_version: 1,
      plugins: [
        {
          plugin_id: 'broken',
          plugin_version: '1.0.0',
          id: 'broken-page',
          nav_key: 'app:broken',
          title: 'Broken',
          render_mode: 'bundled',
          position: 10,
          icon: { href: 'javascript:alert(1)' },
        },
      ],
    }),
    [],
  );
});
