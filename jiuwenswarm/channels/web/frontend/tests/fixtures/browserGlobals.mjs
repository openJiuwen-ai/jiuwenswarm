import { JSDOM } from 'jsdom';

// Browser transport selection runs at module import time, before test setup hooks.
const dom = new JSDOM('', { url: 'http://localhost' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
