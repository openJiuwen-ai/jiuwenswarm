import { build } from 'esbuild';

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
  },
};

await build({
  entryPoints: [
    { in: 'src/components/SkillPanel/MarketplaceView.tsx', out: 'MarketplaceView' },
    { in: 'src/components/SkillPanel/skillPanelUtils.ts', out: 'skillPanelUtils' },
    { in: 'src/components/ui/FilePreviewPanel/FilePreviewContent.tsx', out: 'FilePreviewContent' },
    { in: 'src/components/ui/MarkdownPane/MarkdownPane.tsx', out: 'MarkdownPane' },
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
