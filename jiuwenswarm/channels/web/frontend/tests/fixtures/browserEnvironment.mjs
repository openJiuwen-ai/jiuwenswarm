import { after } from 'node:test';
import { JSDOM } from 'jsdom';

const previousWindow = globalThis.window;
const previousDocument = globalThis.document;
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
after(() => {
  globalThis.window = previousWindow;
  globalThis.document = previousDocument;
  dom.window.close();
});
