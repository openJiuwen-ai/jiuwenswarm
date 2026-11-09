import { build } from 'esbuild';
import { resolve } from 'node:path';

const svgComponentStub = {
  name: 'skill-marketplace-svg-component-stub',
  setup(builder) {
    builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({
      path,
      namespace: 'svg-react-stub',
    }));
    builder.onLoad({ filter: /.*/, namespace: 'svg-react-stub' }, () => ({
      contents: 'export default function SvgStub() { return null; }',
      loader: 'js',
    }));
    builder.onResolve({ filter: /services\/webClient$/ }, ({ importer }) => {
      if (importer.endsWith('useSkillFilesTab.tsx')) {
        return { path: resolve('tests/fixtures/skillDetailWebClientStub.ts') };
      }
      return undefined;
    });
  },
};

await build({
  entryPoints: [
    { in: 'src/components/SkillPanel/MarketplaceView.tsx', out: 'MarketplaceView' },
    { in: 'src/components/SkillPanel/skillPanelUtils.ts', out: 'skillPanelUtils' },
    { in: 'src/components/ui/FilePreviewPanel/FilePreviewContent.tsx', out: 'FilePreviewContent' },
    { in: 'src/components/ui/MarkdownPane/MarkdownPane.tsx', out: 'MarkdownPane' },
    { in: 'src/components/SkillPanel/useSkillFilesTab.tsx', out: 'useSkillFilesTab' },
    { in: 'src/components/SkillPanel/SkillDetailView.tsx', out: 'SkillDetailView' },
    { in: 'tests/fixtures/skillDetailWebClientStub.ts', out: 'skillDetailWebClientStub' },
  ],
  bundle: true,
  splitting: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  jsx: 'automatic',
  outdir: 'node_modules/.cache/skill-marketplace',
  outExtension: { '.js': '.mjs' },
  loader: {
    '.css': 'empty',
    '.png': 'dataurl',
    '.svg': 'dataurl',
  },
  define: {
    'import.meta.env': '{}',
  },
  plugins: [svgComponentStub],
});
