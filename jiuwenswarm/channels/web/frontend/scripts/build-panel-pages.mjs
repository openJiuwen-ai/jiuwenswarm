import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';

// Exercise the real page container while replacing unrelated native bodies and app stores.
const nativeMocks = `
  import { createElement, useRef } from 'react';
  const noop = () => {};
  export const useFullscreenPanel = () => ({
    ref: useRef(null), isFullscreen: false, toggle: noop, enter: noop, exit: noop,
  });
  export const useSessionArtifactsCount = () => 1;
  export const ArtifactExpandedPanel = () => createElement('div', null, 'Native artifacts');
  export const OrgInfoPanel = () => createElement('div', null, 'Native organization');
  export const useTeamOrganizationUiEnabled = () => true;
  export const useChatStore = (select) => select({ activeSessionId: 'session' });
  export const useSessionStore = (select) => select({ runtimes: { session: { mode: 'team' } } });
`;

await build({
  absWorkingDir: fileURLToPath(new URL('..', import.meta.url)),
  entryPoints: ['tests/fixtures/panelPages.ts'],
  outfile: 'node_modules/.cache/panel-pages/components.mjs',
  bundle: true, packages: 'external', platform: 'browser', format: 'esm',
  loader: { '.css': 'empty', '.svg': 'dataurl' },
  plugins: [{
    name: 'panel-pages-native-bodies',
    setup(builder) {
      builder.onResolve({ filter: /.*/ }, ({ path, importer }) => {
        if (importer.endsWith('ExpandedPanel.tsx') && [
          '../../hooks', '../ArtifactsPanel', './OrgInfoPanel',
          '../../features/teamOrganization/featureConfig', '../../stores',
        ].includes(path)) return { path: 'native-mocks', namespace: 'panel-test' };
      });
      builder.onLoad({ filter: /.*/, namespace: 'panel-test' }, () => ({
        contents: nativeMocks, loader: 'js', resolveDir: fileURLToPath(new URL('..', import.meta.url)),
      }));
      builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'svg-stub' }));
      builder.onLoad({ filter: /.*/, namespace: 'svg-stub' }, () => ({ contents: "export default 'svg';", loader: 'js' }));
    },
  }],
});
