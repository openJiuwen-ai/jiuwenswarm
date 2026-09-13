import assert from 'node:assert/strict';
import test from 'node:test';

process.env.NODE_ENV = 'production';

const { createElement } = await import('react');
const { renderToStaticMarkup } = await import('react-dom/server');
const { createInstance } = await import('i18next');
const { I18nextProvider } = await import('react-i18next');
const { ApplicationPluginCard } = await import(
  '../node_modules/.cache/application-plugin-card/ApplicationPluginCard.mjs'
);

const SUMMARY = 'Co-edit cloud documents on Google Docs, Sheets and Slides and on Feishu.';

async function render(plugin, settings) {
  const i18n = createInstance();
  await i18n.init({
    lng: 'en',
    fallbackLng: 'en',
    initImmediate: false,
    resources: {
      en: {
        translation: {
          nav: { docs: 'Docs' },
          docs: { plugin: { summary: SUMMARY } },
          applicationPlugins: {
            enabled: 'Enabled',
            disabled: 'Disabled',
            noSettings: 'This plugin does not provide a settings page.',
            showDescription: 'Show plugin description',
            hideDescription: 'Hide plugin description',
          },
        },
      },
    },
  });
  return renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n },
      createElement(ApplicationPluginCard, {
        plugin,
        settings,
        onRefresh: async () => {},
      }),
    ),
  );
}

const coScribe = {
  plugin_id: 'co-scribe',
  plugin_version: '1.0.0',
  enabled: true,
  id: 'co-scribe-docs',
  nav_key: 'app:co-scribe',
  title: 'Docs',
  title_i18n_key: 'nav.docs',
  description: 'the untranslated fallback',
  description_i18n_key: 'docs.plugin.summary',
  icon: 'data:image/png;base64,iVBORw0KGgo=',
  render_mode: 'bundled',
  position: 80,
};

// video_duplex declares neither, and must keep working untouched.
const videoDuplex = {
  plugin_id: 'video-duplex',
  plugin_version: '1.0.0',
  enabled: true,
  id: 'video-duplex-live',
  nav_key: 'app:video-duplex',
  title: 'Full-duplex Video',
  render_mode: 'bundled',
  position: 75,
};

test('renders the icon a plugin carries', async () => {
  const html = await render(coScribe);
  assert.match(html, /<img src="data:image\/png;base64,iVBORw0KGgo="/);
  assert.match(html, /data-testid="application-plugin-icon"/);
  assert.match(html, /co-scribe · v1\.0\.0/);
  // title_i18n_key still wins over the raw title.
  assert.match(html, /<strong>Docs<\/strong>/);
});

test('the icon is the description disclosure, closed to begin with', async () => {
  const html = await render(coScribe);
  assert.match(html, /<button[^>]*aria-expanded="false"/);
  assert.match(html, /aria-label="Show plugin description"/);
  // A button is keyboard-reachable by construction -- no tabindex needed.
  assert.doesNotMatch(html, /tabindex/i);
  // The description is in the markup but hidden until the icon is clicked,
  // and the control points at it.
  const controls = /aria-controls="([^"]+)"/.exec(html);
  assert.ok(controls, 'the icon must name the region it discloses');
  assert.match(html, new RegExp(`id="${controls[1]}"[^>]*hidden`));
  assert.match(html, new RegExp(escapeRegExp(SUMMARY)));
});

test('the description names what co-scribe does today, not the retired revert', async () => {
  const html = await render(coScribe);
  assert.doesNotMatch(html, /revert/i);
  assert.doesNotMatch(html, /one-click/i);
});

test('a plugin without an icon still renders, with the generic glyph', async () => {
  const html = await render(videoDuplex);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /<svg/, 'the lucide fallback glyph must still be drawn');
  assert.match(html, /video-duplex · v1\.0\.0/);
  assert.match(html, /Full-duplex Video/);
  // Nothing to disclose, so the icon is not a control at all.
  assert.doesNotMatch(html, /<button/);
  assert.doesNotMatch(html, /application-plugin-description/);
  assert.match(html, /This plugin does not provide a settings page\./);
});

test('renders a plugin settings component when one is supplied', async () => {
  const html = await render(coScribe, ({ contribution }) =>
    createElement('span', { 'data-testid': 'stub-settings' }, contribution.plugin_id),
  );
  assert.match(html, /data-testid="stub-settings"/);
  assert.doesNotMatch(html, /does not provide a settings page/);
});

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
