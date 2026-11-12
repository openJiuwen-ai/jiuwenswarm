import { readdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Discover new tests automatically, independent of the shell's glob expansion.
const files = readdirSync(new URL('./', import.meta.url))
  .filter(name => name.endsWith('.test.mjs')).sort()
  .map(name => fileURLToPath(new URL(name, import.meta.url)));
const result = spawnSync(process.execPath, ['--test', '--test-concurrency=1', ...files], { stdio: 'inherit' });
if (result.error) throw result.error;
process.exit(result.status ?? 1);
