#!/usr/bin/env node
/**
 * jiuwenclaw-agentserver 启动脚本 (Node.js 版本)
 * 通过 Node.js child_process 启动
 */

import { spawn } from 'child_process';
import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const SCRIPT_DIR = path.dirname(__filename);
const PORT = process.argv[2] || '18092';
const OHOS_LAUNCHER = path.join(SCRIPT_DIR, 'scripts', 'start-ohos-agentserver.sh');
const isOhos = process.env.JIUWENCLAW_RUNTIME_PLATFORM === 'ohos'
  || process.platform === 'ohos'
  || fs.existsSync('/data/service/hnp');

const env = {
  ...process.env,
};

if (isOhos) {
  env.JIUWENCLAW_RUNTIME_PLATFORM = 'ohos';
}

console.log(`Starting jiuwenclaw-agentserver on port ${PORT}...`);

const command = isOhos ? '/bin/sh' : path.join(SCRIPT_DIR, '.venv', 'bin', 'python');
const args = isOhos
  ? [OHOS_LAUNCHER, PORT]
  : ['-m', 'jiuwenclaw.app_agentserver', '--port', PORT];

const child = spawn(command, args, {
  cwd: SCRIPT_DIR,
  env,
  stdio: 'inherit',
});

child.on('exit', (code) => {
  process.exit(code);
});

child.on('error', (err) => {
  console.error('Failed to start:', err.message);
  process.exit(1);
});
