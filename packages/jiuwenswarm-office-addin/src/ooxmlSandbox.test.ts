import { strFromU8, unzipSync } from 'fflate';
import { describe, expect, it } from 'vitest';

import { FakeDeck } from './fakePowerPoint';
import { nodeSandbox } from './nodeSandbox';
import { SandboxFailed } from './powerPointPackageTools';

const SLIDE = 'ppt/slides/slide1.xml';

/** A slide package as PowerPoint exports one, with a text shape that says Old. */
async function exported(): Promise<string> {
  const deck = new FakeDeck({ slides: [{ id: '256#', shapes: [{ id: '2', type: 'TextBox', text: 'Old' }] }] });
  return deck.run(async context => {
    const result = context.presentation.slides.getItem('256#').exportAsBase64();
    await context.sync();
    return result.value;
  });
}

const part = (base64: string, path: string) => strFromU8(unzipSync(Buffer.from(base64, 'base64'))[path]);

async function failure(promise: Promise<unknown>): Promise<SandboxFailed> {
  try {
    await promise;
  } catch (error) {
    return error as SandboxFailed;
  }
  throw new Error('it did not fail');
}

describe('the sandbox worker', () => {
  const sandbox = nodeSandbox();

  it('runs the code on the package as a JSZip archive, and hands back what it marked dirty and returned', async () => {
    const base64 = await exported();
    const ran = await sandbox(`
      const xml = await zip.file('${SLIDE}').async('string');
      zip.file('${SLIDE}', xml.replace('Old', escapeXml('R&D')));
      markDirty();
      return { parts: Object.keys(zip.files).length, missing: zip.file('ppt/none.xml') };
    `, base64);
    expect(ran).toMatchObject({ dirty: true, result: { parts: Object.keys(unzipSync(Buffer.from(base64, 'base64')))
      .length, missing: null } });
    expect(part(ran.base64, SLIDE)).toContain('<a:t>R&amp;D</a:t>');
  });

  it('keeps the package as it came unless the code calls markDirty, and tells no return from a null one', async () => {
    const base64 = await exported();
    const unmarked = await sandbox(`zip.file('${SLIDE}', '<p:sld/>');`, base64);
    expect(unmarked).toEqual({ base64, dirty: false });
    expect(await sandbox('return null;', base64)).toEqual({ base64, dirty: false, result: null });
  });

  it('gives the code no network, no host and no way back to the worker', async () => {
    const ran = await sandbox(`return [typeof fetch, typeof XMLHttpRequest, typeof WebSocket, typeof postMessage,
      typeof importScripts, typeof self, typeof process, typeof require, typeof Office, typeof PowerPoint,
      (function () { return typeof this; })()];`, await exported());
    expect(ran.result).toEqual(Array(11).fill('undefined'));
    const escapes = ['Function("return fetch")()', 'DOMParser.constructor("return fetch")()',
      'zip.file.constructor("return self")()', '(async () => {}).constructor("return fetch")()'];
    const reached = await sandbox(`return [${escapes.map(escape => `(() => {
      try { return typeof ${escape}; } catch { return 'refused'; } })()`).join(', ')}];`, await exported());
    expect(reached.result).toEqual(['undefined', 'refused', 'refused', 'refused']);
  });

  it('inserts a chart with the pane\'s own builder', async () => {
    const ran = await sandbox(`
      const placed = insertChart(zip, { type: 'column', categories: ['A', 'B'], values: [1, 2] });
      markDirty();
      const xml = buildChart({ type: 'bar', categories: ['A'], values: [1] });
      return { placed, xml: xml.includes('<c:barDir val="bar"/>') };
    `, await exported());
    expect(ran.result).toEqual({ placed: { shape_ids: ['3'], chart_part: 'ppt/charts/chart1.xml' }, xml: true });
    expect(part(ran.base64, 'ppt/charts/chart1.xml')).toContain('<c:barChart>');
    expect(part(ran.base64, '[Content_Types].xml')).toContain('/ppt/charts/chart1.xml');
  });

  it('says what the code got wrong, and how to list the parts when it asks for them by pattern', async () => {
    expect(await failure(sandbox('zip.file(/slide/);', await exported()))).toMatchObject({ failure: 'threw',
      message: 'The code failed: TypeError: zip.file takes a path string; list parts with Object.keys(zip.files)' });
    expect(await failure(sandbox(`await zip.file('${SLIDE}').async('arraybuffer');`, await exported())))
      .toMatchObject({ message: expect.stringMatching(/"string", "uint8array" or "base64"/) });
  });

  it('stops code that runs past its time', async () => {
    expect(await failure(nodeSandbox(1500)('while (true) {}', await exported()))).toMatchObject({
      failure: 'timed_out', message: 'The code ran past 1.5 s and was stopped' });
  });
});
