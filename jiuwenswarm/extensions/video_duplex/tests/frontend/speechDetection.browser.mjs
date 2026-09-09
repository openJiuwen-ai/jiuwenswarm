// Production-asset smoke test. Set PLAYWRIGHT_MODULE to a Playwright ESM entry
// and VAD_SPEECH_WAV to a PCM16 WAV of speech; no microphone or provider is used.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE
  ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : 'playwright');
const dist = process.env.VAD_TEST_DIST
  ? path.resolve(process.env.VAD_TEST_DIST) + path.sep
  : fileURLToPath(new URL('../../../../channels/web/frontend/dist/', import.meta.url));
const workerFile = (await readdir(path.join(dist, 'assets'))).find((name) => /^silero\.worker-.*\.js$/.test(name));
const detectorFile = (await readdir(path.join(dist, 'assets'))).find((name) => /^sileroVad-.*\.js$/.test(name));
assert.ok(workerFile, 'build must include the Silero worker');
const wav = await readFile(process.env.VAD_SPEECH_WAV);
let rate;
let channels;
let audio;
for (let offset = 12; offset + 8 <= wav.length;) {
  const kind = wav.toString('ascii', offset, offset + 4);
  const size = wav.readUInt32LE(offset + 4);
  if (kind === 'fmt ') {
    assert.equal(wav.readUInt16LE(offset + 8), 1, 'PCM WAV required');
    channels = wav.readUInt16LE(offset + 10);
    rate = wav.readUInt32LE(offset + 12);
    assert.equal(wav.readUInt16LE(offset + 22), 16, '16-bit WAV required');
  }
  if (kind === 'data') audio = wav.subarray(offset + 8, offset + 8 + size);
  offset += 8 + size + (size % 2);
}
assert.ok(audio && rate && channels);
const speech = [];
for (let i = 0; i < Math.floor(audio.length / (2 * channels) * 16000 / rate); i++) {
  speech.push(audio.readInt16LE(Math.floor(i * rate / 16000) * channels * 2));
}
const server = createServer(async (req, res) => {
  const name = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
  if (name === '/') { res.setHeader('Content-Type', 'text/html'); res.end('<!doctype html><title>VAD test</title>'); return; }
  const file = path.resolve(dist, '.' + name);
  if (!file.startsWith(dist)) { res.writeHead(403); res.end(); return; }
  try {
    const content = await readFile(file);
    res.setHeader('Content-Type', file.endsWith('.wasm') ? 'application/wasm'
      : /\.m?js$/.test(file) ? 'text/javascript' : 'application/octet-stream');
    res.end(content);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
let browser;
try {
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  const report = await page.evaluate(async ({ workerFile, speech }) => {
    const worker = new Worker(`/assets/${workerFile}`, { type: 'module' });
    const exchange = (message) => new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('VAD worker timeout')), 30000);
      worker.onerror = (event) => { clearTimeout(timeout); reject(new Error(event.message)); };
      worker.onmessage = ({ data }) => {
        clearTimeout(timeout);
        if (data.type === 'error') reject(new Error(data.message)); else resolve(data);
      };
      worker.postMessage(message);
    });
    try {
      const start = performance.now();
      await exchange({ type: 'init' });
      const loadMs = performance.now() - start;
      const run = async (samples) => {
        const detections = [];
        let maxBatchMs = 0;
        for (let i = 0; i < samples.length; i += 1600) {
          const start = performance.now();
          const data = await exchange({ type: 'audio', pcm: samples.slice(i, i + 1600).buffer, capturedAt: start });
          maxBatchMs = Math.max(maxBatchMs, performance.now() - start);
          detections.push(...data.detections);
        }
        return {
          starts: detections.filter((d) => d.state === 'started').length,
          maxProbability: Math.max(...detections.map((d) => d.probability)),
          maxBatchMs,
        };
      };
      const silence = await run(new Int16Array(32000));
      let seed = 1234;
      const noise = Int16Array.from({ length: 48000 }, () => {
        seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
        return (seed / 4294967296 - 0.5) * 20000;
      });
      const loudNoise = await run(noise);
      await run(new Int16Array(16000));
      const voice = await run(Int16Array.from(speech));
      return { loadMs, silence, loudNoise, voice };
    } finally { worker.terminate(); }
  }, { workerFile, speech });
  assert.deepEqual(errors, []);
  assert.equal(report.silence.starts, 0);
  assert.equal(report.loudNoise.starts, 0);
  assert.ok(report.voice.starts >= 1, 'real Silero inference must recognize the speech fixture');
  assert.ok(report.voice.maxBatchMs < 500, 'inference must not trigger stale-result protection');
  const recovery = await page.evaluate(async ({ detectorFile, speech }) => {
    const { SileroVad } = await import(`/assets/${detectorFile}`);
    const detections = [];
    const diagnostics = [];
    const errors = [];
    const vad = new SileroVad(
      (event) => detections.push(event),
      (message) => errors.push(message),
      (event) => diagnostics.push(event),
    );
    try {
      await vad.start();
      // Reproduce AudioWorklet messages arriving in a burst after a UI-thread stall.
      for (let i = 0; i < 12; i++) vad.push(new Int16Array(1600));
      for (let i = 0; i < Math.min(speech.length, 96000); i += 1600) {
        await new Promise((resolve) => setTimeout(resolve, 100));
        vad.push(Int16Array.from(speech.slice(i, i + 1600)));
      }
      await new Promise((resolve) => setTimeout(resolve, 200));
      return { errors, diagnostics, speechStarts: detections.filter((event) => event.state === 'started').length };
    } finally { vad.stop(); }
  }, { detectorFile, speech });
  assert.deepEqual(recovery.errors, []);
  assert.ok(recovery.diagnostics.includes('qwen_vad_resync'));
  assert.ok(recovery.diagnostics.includes('qwen_vad_recovered'));
  assert.ok(recovery.speechStarts > 0, 'real speech must still be detected after an audio backlog');
  console.log(JSON.stringify({ ...report, recovery }, null, 2));
} finally {
  await browser?.close();
  await new Promise((resolve) => server.close(resolve));
}
