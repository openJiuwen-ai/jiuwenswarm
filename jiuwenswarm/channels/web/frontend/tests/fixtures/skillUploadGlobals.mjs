import { JSDOM } from 'jsdom';

// Browser transport selection runs at module import time, before test setup hooks.
const dom = new JSDOM('', { url: 'http://localhost' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.localStorage = dom.window.localStorage;

// Node has File/Blob but no FileReader; enterpriseSkillUpload reads ZIPs as data URLs.
class TestFileReader {
  result = null;
  error = null;
  onload = null;
  onerror = null;

  readAsDataURL(file) {
    file.arrayBuffer().then(
      (buffer) => {
        const base64 = Buffer.from(buffer).toString('base64');
        this.result = `data:${file.type || 'application/zip'};base64,${base64}`;
        this.onload?.();
      },
      (error) => {
        this.error = error;
        this.onerror?.();
      },
    );
  }
}

globalThis.FileReader = TestFileReader;
