/**
 * The pane's sandbox for the tests: ``ooxmlSandbox.worker`` bundled with esbuild, as Vite bundles it for the pane,
 * and run in a node worker thread that stands in for the pane's module Worker.
 */
import { fileURLToPath } from 'node:url';
import { Worker } from 'node:worker_threads';

import { buildSync } from 'esbuild';

import { workerSandbox, type Sandbox, type SandboxWorker } from './powerPointPackageTools';

let source: string | undefined;

function bundle(): string {
  const worker = fileURLToPath(new URL('./ooxmlSandbox.worker.ts', import.meta.url));
  const [output] = buildSync({ entryPoints: [worker], bundle: true, write: false, format: 'esm', platform: 'browser' })
    .outputFiles;
  return 'import { parentPort } from \'node:worker_threads\';\n'
    + 'globalThis.postMessage = value => parentPort.postMessage(value);\n'
    + 'parentPort.on(\'message\', data => globalThis.onmessage({ data }));\n'
    + output.text;
}

export function nodeSandbox(timeoutMs?: number): Sandbox {
  source ??= bundle();
  const url = new URL(`data:text/javascript,${encodeURIComponent(source)}`);
  return workerSandbox(() => {
    const thread = new Worker(url);
    const worker: SandboxWorker = { onmessage: null, onerror: null, postMessage: request => thread.postMessage(request),
      terminate: () => void thread.terminate() };
    thread.on('message', data => worker.onmessage?.({ data } as MessageEvent));
    thread.on('error', error => worker.onerror?.({ message: error.message } as ErrorEvent));
    return worker;
  }, timeoutMs);
}
