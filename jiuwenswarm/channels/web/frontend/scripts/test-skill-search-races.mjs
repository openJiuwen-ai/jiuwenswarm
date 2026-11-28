import { build } from 'esbuild';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
await build({
  absWorkingDir: root,
  entryPoints: [
    'src/features/ClawHubSearchModal/index.tsx',
    'src/features/SkillNetSearchModal/index.tsx',
    'src/features/TeamSkillsHubModal/index.tsx',
    'src/features/EnterpriseSkillSourcePanel/index.tsx',
    'src/components/ui/FilePreviewPanel/FilePreviewContent.tsx',
  ],
  outbase: 'src',
  outdir: 'node_modules/.cache/skill-search-races',
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  define: { 'import.meta.env': '{"DEV":false}' },
  plugins: [
    {
      name: 'skill-search-races-test-stubs',
      setup(builder) {
        const STUB_FILTER = /(\.svg\?react$|\/services\/webClient$|\/utils\/writeClipboard$|\/MarkdownRenderer$|\/CodePreview$)/;
        builder.onResolve({ filter: STUB_FILTER }, ({ path }) => ({ path, namespace: 'races-stub' }));
        builder.onLoad({ filter: /.*/, namespace: 'races-stub' }, ({ path }) => {
          if (/\.svg\?react$/.test(path)) {
            return { contents: 'export default function SvgStub() { return null; }', loader: 'js' };
          }
          if (/\/services\/webClient$/.test(path)) {
            return {
              loader: 'js',
              contents: `
                export async function webRequest(method, params) {
                  const impl = globalThis.__webRequestImpl;
                  if (!impl) throw new Error('webRequest stub not configured');
                  return impl(method, params);
                }
                // The enterprise panel's import graph (SkillPackInstallPlan →
                // useWebSocket) also pulls these named exports; unused in the
                // race tests, so stub them as inert values.
                export const webClient = {
                  on: () => () => {},
                  off: () => {},
                  send: () => {},
                  close: () => {},
                };
                export const PAUSABLE_STREAM_EVENTS = new Set();
                export async function requestGoalAction() { throw new Error('not used in tests'); }
                export async function sendGoalStreamCommand() { throw new Error('not used in tests'); }
                export async function requestWorkflowList() { return { success: false }; }
                export async function requestWorkflowDetail() { return { success: false }; }
                export async function requestPhaseAgents() { return { success: false }; }
                export async function requestAgentDetail() { return { success: false }; }
              `,
            };
          }
          if (/\/utils\/writeClipboard$/.test(path)) {
            return {
              loader: 'js',
              contents: `
                export async function writeClipboard(text) {
                  const impl = globalThis.__writeClipboardImpl;
                  if (!impl) throw new Error('writeClipboard stub not configured');
                  return impl(text);
                }
              `,
            };
          }
          if (/\/MarkdownRenderer$/.test(path)) {
            return { contents: 'export function MarkdownRenderer() { return null; }', loader: 'js' };
          }
          return { contents: 'export function CodePreview() { return null; }', loader: 'js' };
        });
      },
    },
  ],
});
const testArgs = [
  '--test',
  ...process.argv.slice(2),
  'tests/clawHubSearchModal.test.mjs',
  'tests/filePreviewContent.test.mjs',
  'tests/skillSourceModalsEmbedded.test.mjs',
  'tests/enterpriseSourcePanel.test.mjs',
];
const result = spawnSync(process.execPath, testArgs, { cwd: root, stdio: 'inherit' });
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
