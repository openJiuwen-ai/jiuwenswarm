import assert from 'node:assert/strict';
import test from 'node:test';

import { isEmbeddableDocUrl } from '../../../../channels/web/frontend/node_modules/.cache/clouddoc-doc-url/extensions/co_scribe/frontend/features/clouddoc/docUrl.js';

// The frame carries the person's platform login and the clipboard. Only the
// platforms' own hosts are framed; anything else is launched externally.
test('platform hosts are embeddable', () => {
  assert.equal(isEmbeddableDocUrl('https://docs.google.com/document/d/x/edit'), true);
  assert.equal(isEmbeddableDocUrl('https://drive.google.com/file/d/x/view'), true);
  assert.equal(isEmbeddableDocUrl('https://acme.feishu.cn/docx/x'), true);
  assert.equal(isEmbeddableDocUrl('https://feishu.cn/docx/x'), true);
  assert.equal(isEmbeddableDocUrl('https://acme.larksuite.com/docx/x'), true);
  assert.equal(isEmbeddableDocUrl('https://acme.larkoffice.com/wiki/x'), true);
});

test('foreign, look-alike and insecure origins are not', () => {
  assert.equal(isEmbeddableDocUrl('https://evil.example/document/d/x/edit'), false);
  assert.equal(isEmbeddableDocUrl('https://docs.google.com.evil.example/document/d/x'), false);
  assert.equal(isEmbeddableDocUrl('https://evilfeishu.cn/docx/x'), false);
  assert.equal(isEmbeddableDocUrl('http://docs.google.com/document/d/x/edit'), false);
  assert.equal(isEmbeddableDocUrl('javascript:alert(1)'), false);
  assert.equal(isEmbeddableDocUrl('not a url'), false);
  assert.equal(isEmbeddableDocUrl(''), false);
  assert.equal(isEmbeddableDocUrl(undefined), false);
});
