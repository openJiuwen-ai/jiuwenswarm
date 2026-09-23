import { execFileSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { ESLint } from 'eslint';

// Compare with the same rules on the base revision. Existing diagnostics remain
// visible through npm run lint; new errors AND warnings fail this incremental check.
const cwd = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const git = (...args) => execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
const args = process.argv.slice(2);
if (args.length && (args.length !== 2 || args[0] !== '--base')) {
  throw new Error('Usage: npm run lint:changed -- [--base <commit>]');
}
const base = git('rev-parse', '--verify', `${args[1] ?? 'HEAD'}^{commit}`).trim();
const repoRoot = git('rev-parse', '--show-toplevel').trim();
const prefix = path.relative(repoRoot, cwd).split(path.sep).join('/') + '/';
const changed = new Set([
  ...git('diff', '--name-only', '--diff-filter=ACMR', '-z', base, '--', '.').split('\0'),
  ...git('ls-files', '--others', '--exclude-standard', '-z', '--', '.').split('\0'),
]);
const eslint = new ESLint({ cwd, reportUnusedDisableDirectives: 'error' });
const introduced = [];
let checked = 0;
let existing = 0;
const diagnosticKey = (message) => JSON.stringify([message.ruleId, message.severity, message.message]);

for (const file of [...changed].sort()) {
  // git diff emits repository-relative paths, while ls-files uses cwd-relative paths.
  const relative = file.startsWith(prefix) ? file.slice(prefix.length) : file;
  if (!/\.tsx?$/.test(relative)) continue;
  const absolute = path.resolve(cwd, relative);
  if (!absolute.startsWith(cwd + path.sep) || (await eslint.isPathIgnored(absolute))) continue;
  const current = await readFile(absolute, 'utf8');
  let previous = '';
  try {
    previous = git('show', `${base}:${prefix}${relative}`);
  } catch (error) {
    // Only an absent base path is a new file; other Git failures must not weaken the check.
    if (
      !String(error.stderr).includes('does not exist in') &&
      !String(error.stderr).includes('exists on disk, but not in')
    ) {
      throw error;
    }
  }
  const [baseResult] = await eslint.lintText(previous, { filePath: absolute });
  const [currentResult] = await eslint.lintText(current, { filePath: absolute });
  const counts = new Map();
  for (const message of baseResult.messages) {
    const key = diagnosticKey(message);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const messages = currentResult.messages.filter((message) => {
    const key = diagnosticKey(message);
    const remaining = counts.get(key) ?? 0;
    if (!remaining) return true;
    counts.set(key, remaining - 1);
    existing += 1;
    return false;
  });
  if (messages.length) {
    introduced.push({
      ...currentResult,
      messages,
      errorCount: messages.filter((message) => message.severity === 2).length,
      warningCount: messages.filter((message) => message.severity === 1).length,
      fixableErrorCount: messages.filter((message) => message.severity === 2 && message.fix).length,
      fixableWarningCount: messages.filter((message) => message.severity === 1 && message.fix).length,
    });
  }
  checked += 1;
}

if (introduced.length) {
  console.log((await eslint.loadFormatter('stylish')).format(introduced));
  process.exitCode = 1;
}
console.log(
  `ESLint: ${checked} changed TS/TSX files; ${introduced.reduce((sum, row) => sum + row.messages.length, 0)} new diagnostics; ${existing} pre-existing diagnostics. Base: ${base.slice(0, 9)}.`,
);
