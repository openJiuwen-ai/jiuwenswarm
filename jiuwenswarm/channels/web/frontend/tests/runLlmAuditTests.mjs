import { build } from 'esbuild';
import { spawnSync } from 'node:child_process';

for (const transport of ['http', 'websocket']) {
  await build({
    entryPoints: ['src/components/ChatPanel/LlmCallAudit.tsx'],
    bundle: true,
    platform: 'node',
    format: 'esm',
    packages: 'external',
    jsx: 'automatic',
    loader: { '.css': 'empty' },
    define: { 'import.meta.env': JSON.stringify({ VITE_WEB_TRANSPORT: transport }) },
    outfile: `node_modules/.cache/llm-audit/${transport}.mjs`,
  });
  const result = spawnSync(process.execPath, ['--test', 'tests/llmAudit.test.mjs'], {
    env: { ...process.env, AUDIT_TEST_TRANSPORT: transport },
    stdio: 'inherit',
  });
  if (result.status !== 0) process.exit(result.status || 1);
}
