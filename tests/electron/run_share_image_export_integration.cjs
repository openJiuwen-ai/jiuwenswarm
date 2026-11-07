const { spawn } = require('node:child_process');
const net = require('node:net');
const path = require('node:path');

(async () => {
  const executable = process.env.SWARM_TEST_ELECTRON_EXECUTABLE
    || require('../../jiuwenswarm/channels/desktop/electron/node_modules/electron');
  const reservation = net.createServer();
  await new Promise((resolve, reject) => {
    reservation.once('error', reject);
    reservation.listen(0, '127.0.0.1', resolve);
  });
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  const env = { ...process.env, JIUWENSWARM_ELECTRON_CDP_PORT: String(port) };
  delete env.ELECTRON_RUN_AS_NODE;
  const child = spawn(executable, [path.join(__dirname, 'share_image_export.integration.cjs')], { env, stdio: 'inherit' });
  for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => child.kill(signal));
  process.exitCode = await new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', code => resolve(code ?? 1));
  });
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
