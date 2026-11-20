import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';
import * as esbuild from 'esbuild';
import i18next from 'i18next';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const svgReactPlugin = {
  name: 'svg-react',
  setup(build) {
    build.onResolve({ filter: /\.svg\?react$/ }, (args) => ({
      path: path.resolve(args.resolveDir, args.path.replace(/\?react$/, '')),
      namespace: 'svg-react',
    }));
    build.onLoad({ filter: /\.svg$/, namespace: 'svg-react' }, () => ({
      contents: 'export default function Svg() { return null; }',
      loader: 'js',
    }));
  },
};

await esbuild.build({
  absWorkingDir: frontendRoot,
  entryPoints: ['src/features/settings/modules/channels/components/ChannelListSection.tsx'],
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outfile: 'node_modules/.cache/settings-channel-icons/ChannelListSection.js',
  loader: { '.svg': 'dataurl' },
  plugins: [svgReactPlugin],
  jsx: 'automatic',
  define: { 'import.meta.env.DEV': 'false' },
});

const { ChannelListSection } = await import(
  '../node_modules/.cache/settings-channel-icons/ChannelListSection.js'
);
const zh = JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8'));
const i18n = i18next.createInstance();
await i18n.use(initReactI18next).init({
  lng: 'zh',
  resources: { zh: { translation: zh } },
  interpolation: { escapeValue: false },
});

test('unconfigured DingTalk channel displays its platform logo', () => {
  const disabledChannels = {
    xiaoyi: false,
    feishu: false,
    dingtalk: false,
    telegram: false,
    discord: false,
    slack: false,
    whatsapp: false,
  };
  const markup = renderToStaticMarkup(
    React.createElement(
      I18nextProvider,
      { i18n },
      React.createElement(ChannelListSection, {
        channels: [{ channel_id: 'dingtalk', logo_src: '/assets/dingtalk.svg', enabled: false }],
        feishuApps: [],
        feishuLoaded: true,
        dingtalk: { apps: [], loaded: true, onAdd() {} },
        loading: false,
        channelConfigured: disabledChannels,
        channelEnabled: disabledChannels,
        savingChannels: disabledChannels,
        onConfigure() {},
        onEdit() {},
        onAddFeishu() {},
        onToggleEnabled() {},
        onUnbind() {},
      }),
    ),
  );
  const document = new JSDOM(markup).window.document;
  const card = document.querySelector('[data-testid="settings-channels-panel-channel"][data-variant="dingtalk"]');

  assert.ok(card, 'the DingTalk channel card should be rendered');
  assert.ok(
    card.querySelector('img[data-testid="settings-channels-panel-logo"][data-variant="dingtalk"]'),
    'the DingTalk channel card should display its own platform logo before an account is configured',
  );
});
