import assert from 'node:assert/strict';
import { spawn as spawnProcess } from 'node:child_process';
import { randomUUID, timingSafeEqual } from 'node:crypto';
import { EventEmitter, once } from 'node:events';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { PassThrough } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import test from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { deflateSync } from 'node:zlib';
import { transformSync } from 'esbuild';
import { PNG_SIGNATURE, buildPngChunk } from '../node_modules/.cache/share-image/streamingPng.js';

const frontend = fileURLToPath(new URL('..', import.meta.url));
const config = fs.readFileSync(path.join(frontend, 'vite.config.ts'), 'utf8');
// Run the real middleware with a shorter idle timeout and an observable job registry.
const source = config.slice(config.indexOf('function devFileContentApi()'), config.indexOf('// https://vitejs.dev/config/'))
  .replace('const shareImageJobs = new Map<string, DevShareImageJob>()', 'const shareImageJobs = testJobs')
  .replace('const shareImageJobTtlMs = 60 * 60 * 1000', 'const shareImageJobTtlMs = 300');
const compiled = transformSync(source, { loader: 'ts', format: 'cjs' }).code;

function png() {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(2250, 0);
  header.writeUInt32BE(1, 4);
  header[8] = 8;
  header[9] = 6;
  return Buffer.concat([PNG_SIGNATURE, buildPngChunk('IHDR', header),
    buildPngChunk('IDAT', deflateSync(Buffer.alloc(9001))), buildPngChunk('IEND', new Uint8Array())]);
}

async function waitUntil(predicate) {
  const end = Date.now() + 5000;
  while (!predicate()) {
    assert.ok(Date.now() < end, 'condition did not become true');
    await delay(5);
  }
}

async function harness(t, { holdValidation = false, validationExitCode = 0, requestTimeout = 200, otherHandler } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'share-upload-test-'));
  const session = path.join(root, 'agent/sessions/remote');
  fs.mkdirSync(session, { recursive: true });
  fs.writeFileSync(path.join(session, 'history.json'), JSON.stringify([{ role: 'user', content: 'export' }]));
  const jobs = new Map();
  const children = new Set();
  let releaseValidation;
  let middleware;
  const uploadSockets = [];
  const server = http.createServer((req, res) => {
    if (!req.url.startsWith('/share-api/')) {
      if (otherHandler) otherHandler(req, res);
      else {
        req.resume();
        req.on('end', () => res.end('{}'));
      }
      return;
    }
    if (req.url.includes('/browser-result')) uploadSockets.push(req.socket);
    middleware(req, res, () => res.end());
  });
  server.requestTimeout = requestTimeout;
  const spawn = (_command, args, options) => {
    if (args.includes('--validate-result') && !holdValidation) {
      // Exercise real child close events without requiring a Python environment
      // for frontend tests. PNG/ZIP validation is covered by the Python suite.
      const child = spawnProcess(process.execPath, ['-e', 'process.exit(Number(process.argv[1]))',
        String(validationExitCode)], options);
      children.add(child);
      child.once('close', () => children.delete(child));
      return child;
    }
    const child = new EventEmitter();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.kill = () => child.emit('close', 1);
    if (args.includes('--validate-result')) {
      releaseValidation = () => child.emit('close', 0);
    } else {
      queueMicrotask(() => {
        child.stdout.emit('data', Buffer.from('{"renderer":"browser","phase":"snapshot_ready"}\n'));
        child.emit('close', 0);
      });
    }
    return child;
  };
  const context = vm.createContext({
    fs, os, path, process, Buffer, URL, randomUUID, timingSafeEqual, pipeline, spawn,
    setTimeout, clearTimeout, __dirname: frontend, resolveProjectRootDir: () => root, testJobs: jobs,
  });
  vm.runInContext(compiled, context);
  context.devFileContentApi().configureServer({
    httpServer: server, config: { logger: { error: message => assert.fail(message) } },
    middlewares: { use: (...args) => { if (!middleware) middleware = args[0]; } },
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const base = `http://127.0.0.1:${server.address().port}`;
  t.after(async () => {
    releaseValidation?.();
    server.closeAllConnections();
    await waitUntil(() => [...jobs.values()].every(job => !job.uploadInProgress));
    await new Promise(resolve => server.close(resolve));
    assert.equal(children.size, 0);
    fs.rmSync(root, { recursive: true, force: true });
  });
  const response = await fetch(`${base}/share-api/jobs`, {
    method: 'POST', body: JSON.stringify({ session_id: 'remote', allow_browser_render: true }),
  });
  const created = await response.json();
  const job = jobs.get(created.job_id);
  assert.equal(job.renderer, 'browser');
  function upload(size) {
    const req = http.request(`${base}/share-api/jobs/${job.jobId}/browser-result?filename=share.png`, {
      method: 'POST', headers: { 'Content-Length': size, 'X-Share-Export-Token': created.client_token },
    });
    const result = new Promise(resolve => {
      req.once('error', error => resolve({ error }));
      req.once('response', res => {
        const chunks = [];
        res.on('data', chunk => chunks.push(chunk));
        res.on('end', () => resolve({ status: res.statusCode, body: JSON.parse(Buffer.concat(chunks)) }));
      });
    });
    req.flushHeaders();
    return { req, result };
  }
  return { job, jobs, base, server, upload, uploadSockets, release: () => releaseValidation?.() };
}

for (const route of ['/share-api/jobs', '/share-api/jobs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/browser', '/file-api/upload']) {
  for (const trickle of [false, true]) {
    test(`dev request deadline closes ${route} with ${trickle ? 'continuous data' : 'a stalled body'}`, async t => {
      const h = await harness(t);
      const req = http.request(`${h.base}${route}`, { method: 'POST', headers: { 'Content-Length': 1024 } });
      req.on('error', () => {});
      const response = once(req, 'response');
      req.write('{');
      const writer = trickle ? setInterval(() => req.write(' '), 25) : null;
      t.after(() => { clearInterval(writer); req.destroy(); });
      const received = await Promise.race([response, delay(1000).then(() => null)]);
      assert.ok(received, 'incomplete request must time out');
      const [res] = received;
      res.resume();
      assert.equal(res.statusCode, 408);
      assert.equal(res.headers.connection, 'close');
      await waitUntil(() => req.destroyed);
      clearInterval(writer);
    });
  }
}

test('dev request deadline stops after receiving the body and permits a slow response', async t => {
  const h = await harness(t, {
    otherHandler: (req, res) => {
      req.resume();
      req.on('end', () => setTimeout(() => res.end('received'), 400));
    },
  });
  const response = await fetch(`${h.base}/slow-response`, { method: 'POST', body: 'complete' });
  assert.equal(response.status, 200);
  assert.equal(await response.text(), 'received');
});

test('dev request deadline closes an incomplete body after response headers were sent', async t => {
  const h = await harness(t, {
    otherHandler: (req, res) => { req.resume(); res.write('started'); },
  });
  const req = http.request(`${h.base}/early-response`, { method: 'POST', headers: { 'Content-Length': 1024 } });
  req.on('error', () => {});
  t.after(() => req.destroy());
  const response = once(req, 'response');
  req.write('{');
  const [res] = await response;
  res.on('error', () => {});
  res.resume();
  assert.equal(res.statusCode, 200);
  await waitUntil(() => req.destroyed && res.aborted);
});

test('dev upload survives continuous trickles and concurrent expiry checks', async t => {
  const h = await harness(t);
  const data = png();
  const { req, result } = h.upload(data.length);
  const ordinary = http.request(`${h.base}/share-api/jobs`, {
    method: 'POST', headers: { 'Content-Length': 1024 },
  });
  ordinary.on('error', () => {});
  const ordinaryResponse = once(ordinary, 'response');
  ordinary.write('{');
  t.after(() => ordinary.destroy());
  assert.equal(h.server.requestTimeout, 0);
  await waitUntil(() => h.job.uploadInProgress);
  for (let offset = 0; offset < data.length; offset += 4) {
    req.write(data.subarray(offset, offset + 4));
    if (offset + 4 < data.length) {
      await delay(25);
      h.job.updatedAt = 0;
      const status = await fetch(`${h.base}/share-api/jobs/${h.job.jobId}`);
      assert.equal(status.status, 200);
    }
  }
  req.end();
  assert.equal((await result).status, 200);
  const [timedOut] = await ordinaryResponse;
  timedOut.resume();
  assert.equal(timedOut.statusCode, 408);
  await waitUntil(() => ordinary.destroyed);
  await waitUntil(() => !h.job.uploadInProgress);
  assert.equal(h.job.state, 'completed');
  assert.deepEqual(fs.readFileSync(h.job.resultPath), data);
  assert.deepEqual(fs.readdirSync(h.job.directory).sort(), [h.job.filename, 'snapshot.json'].sort());
});

for (const initial of ['', 'partial']) {
  test(`dev stalled upload closes its socket and deletes partial files (${initial || 'no bytes'})`, async t => {
    const h = await harness(t);
    const { req, result } = h.upload(1024 * 1024);
    if (initial) req.write(initial);
    await waitUntil(() => h.job.uploadInProgress);
    h.job.updatedAt = 0;
    assert.equal((await fetch(`${h.base}/share-api/jobs/${h.job.jobId}`)).status, 200);
    const failure = await result;
    assert.ok(failure.error || failure.status === 400);
    if (failure.body) assert.equal(failure.body.error, 'share_export_upload_failed');
    await waitUntil(() => req.destroyed && h.uploadSockets.every(socket => socket.destroyed));
    await waitUntil(() => !h.job.uploadInProgress);
    assert.equal(h.job.state, 'failed');
    assert.deepEqual(fs.readdirSync(h.job.directory), ['snapshot.json']);
    const response = await fetch(`${h.base}/share-api/jobs?session_id=remote`);
    assert.equal(await response.json(), null);
    h.job.updatedAt = 0;
    assert.equal((await fetch(`${h.base}/share-api/jobs/${h.job.jobId}`)).status, 404);
    assert.equal(fs.existsSync(h.job.directory), false);
  });
}

test('dev validation retains file ownership and rejects a simultaneous upload', async t => {
  const h = await harness(t, { holdValidation: true });
  const data = png();
  const first = h.upload(data.length);
  first.req.end(data);
  await waitUntil(() => fs.readdirSync(h.job.directory).some(name => name.endsWith('.png')));
  h.job.updatedAt = 0;
  assert.equal((await fetch(`${h.base}/share-api/jobs/${h.job.jobId}`)).status, 200);
  const second = h.upload(data.length);
  second.req.end(data);
  assert.equal((await second.result).status, 409);
  h.release();
  assert.equal((await first.result).status, 200);
  await waitUntil(() => !h.job.uploadInProgress);
  assert.deepEqual(fs.readFileSync(h.job.resultPath), data);
});

test('dev peer disconnect aborts the writer and releases the conversation', async t => {
  const h = await harness(t);
  const { req, result } = h.upload(1024 * 1024);
  req.write('partial');
  await waitUntil(() => h.job.uploadInProgress);
  req.destroy();
  await result;
  await waitUntil(() => !h.job.uploadInProgress);
  assert.equal(h.job.state, 'failed');
  assert.deepEqual(fs.readdirSync(h.job.directory), ['snapshot.json']);
});


test('dev failed validator leaves no temporary artifact or child process', async t => {
  const h = await harness(t, { validationExitCode: 1 });
  const { req, result } = h.upload(7);
  req.end('invalid');
  assert.equal((await result).status, 400);
  await waitUntil(() => !h.job.uploadInProgress);
  assert.equal(h.job.state, 'failed');
  assert.deepEqual(fs.readdirSync(h.job.directory), ['snapshot.json']);
});

test('dev server shutdown waits for validation to release its files', async t => {
  const h = await harness(t, { holdValidation: true });
  const data = png();
  const { req, result } = h.upload(data.length);
  req.end(data);
  await waitUntil(() => fs.readdirSync(h.job.directory).some(name => name.endsWith('.png')));
  req.destroy();
  await result;
  h.server.closeAllConnections();
  await new Promise(resolve => h.server.close(resolve));
  assert.equal(h.job.uploadInProgress, true);
  assert.equal(fs.existsSync(h.job.directory), true);
  h.release();
  await waitUntil(() => !h.job.uploadInProgress);
  assert.equal(fs.existsSync(h.job.directory), false);
});
