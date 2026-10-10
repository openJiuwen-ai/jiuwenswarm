// Java owns its workspace initialization. Never run Python's generator against it.
if (process.env.JIUWENSWARM_BACKEND !== 'java') {
  const path = require('node:path');
  const { spawnSync } = require('node:child_process');
  const root = path.resolve(__dirname, '../../../../..');
  const result = spawnSync(process.execPath, ['./jiuwenswarm/scripts/generate-agent-folders.js'], {
    cwd: root,
    stdio: 'inherit',
    env: process.env,
  });
  if (result.error) throw result.error;
  process.exit(result.status ?? 1);
}
