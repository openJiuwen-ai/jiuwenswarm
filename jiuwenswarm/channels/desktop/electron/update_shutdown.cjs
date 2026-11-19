const fs = require('node:fs/promises');
const path = require('node:path');
const { randomUUID } = require('node:crypto');

async function createShutdownReceipt(directory) {
  await fs.mkdir(directory, { recursive: true });
  const receipt = path.join(directory, `shutdown-${randomUUID()}.result`);
  await fs.writeFile(receipt, 'pending\n', { flag: 'wx', mode: 0o600 });
  return receipt;
}

async function completeShutdownReceipt(receipt, ok) {
  const previous = await fs.readFile(receipt, 'utf8');
  if (!['pending\n', 'ok\n', 'failed\n'].includes(previous) || (ok && previous === 'failed\n')) {
    throw new Error('Update shutdown receipt cannot be marked successful');
  }
  const temporary = `${receipt}.${randomUUID()}.tmp`;
  try {
    await fs.writeFile(temporary, ok ? 'ok\n' : 'failed\n', { flag: 'wx', mode: 0o600 });
    await fs.rename(temporary, receipt);
  } finally {
    await fs.rm(temporary, { force: true });
  }
}

async function recordShutdownOutcome(receipt, shutdown) {
  try {
    await shutdown();
  } catch (error) {
    if (receipt) {
      await completeShutdownReceipt(receipt, false).catch(receiptError =>
        console.error('[electron] failed to record shutdown failure', receiptError));
    }
    throw error;
  }
  if (receipt) await completeShutdownReceipt(receipt, true);
}

module.exports = { createShutdownReceipt, completeShutdownReceipt, recordShutdownOutcome };
