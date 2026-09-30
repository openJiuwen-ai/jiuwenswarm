import assert from 'node:assert/strict';
import test from 'node:test';
import { readdirSync, readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

// Run after npm run build: exercise emitted assets, not source-level URL strings.
test('production JoyAI and realtime audio worklet entries register without unresolved imports', () => {
  const dist = new URL('../../../../channels/web/frontend/dist/', import.meta.url);
  const registered = [];
  let entries = 0;
  for (const file of readdirSync(new URL('assets/', dist))) {
    if (!file.endsWith('.js')) continue;
    const code = readFileSync(new URL('assets/' + file, dist), 'utf8');
    for (const match of code.matchAll(/audioWorklet\.addModule\(new URL\("([^"]+)",import\.meta\.url\)\)/g)) {
      const url = match[1];
      const body = url.startsWith('data:')
        ? Buffer.from(url.slice(url.indexOf(',') + 1), 'base64').toString('utf8')
        : readFileSync(new URL(url.replace(/^\//, ''), dist), 'utf8');
      if (!body.includes('jiuwen-duplex') && !body.includes('realtime/audio/duplex-')) continue;
      entries += 1;
      // These worklets must be self-contained because Vite may inline small assets
      // as data URLs, which cannot resolve relative module specifiers.
      runInNewContext(body, {
        AudioWorkletProcessor: class {},
        registerProcessor: name => registered.push(name),
      }, { filename: file + ':worklet' });
    }
  }
  assert.ok(entries >= 3, 'Expected emitted JoyAI capture, realtime capture and playback entries');
  assert.equal(registered.filter(name => name === 'jiuwen-duplex-capture').length, 2);
  assert.equal(registered.filter(name => name === 'jiuwen-duplex-playback').length, 1);
});
