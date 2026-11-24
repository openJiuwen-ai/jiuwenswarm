// 通用资产导出按钮组件测试（JSDOM + 注入传输依赖）。
// 组件产物由 npm run test:asset-transfer-ui 经 esbuild 打包后导入；
// i18next 以空资源初始化，t(key) 原样返回 key，据此断言文案分支。
import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  sessionStorage: dom.window.sessionStorage,
  CustomEvent: dom.window.CustomEvent,
  IS_REACT_ACT_ENVIRONMENT: true,
});
const { createRoot } = await import('react-dom/client');
await i18next
  .use(initReactI18next)
  .init({ lng: 'en', showSupportNotice: false, resources: { en: { translation: {} } } });

const { AssetExportButton } =
  await import('../node_modules/.cache/asset-transfer-ui/components/AssetTransfer/AssetExportButton.js');
const { toast, toastStore, TOAST_EXIT_ANIMATION_MS } =
  await import('../node_modules/.cache/asset-transfer-ui/components/ui/Toast/toastStore.js');

// 组件 bundle 经 webClient 链会再次 init i18next 并载入真实 zh/en 资源，
// 最终语言与资源以共享实例为准：断言统一与 tt(key) 比对，不锁定具体文案。
const tt = (key) => i18next.t(key);

const tick = () =>
  act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
const find = (id) => document.querySelector(`[data-testid="${id}"]`);
const click = async (element) => {
  await act(async () => {
    element.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    await new Promise((r) => setTimeout(r, 0));
  });
};
const clearToasts = async () => {
  toast.closeAll();
  await new Promise((r) => setTimeout(r, TOAST_EXIT_ANIMATION_MS + 50));
};
const lastToast = () => {
  const records = toastStore.getSnapshot();
  return records[records.length - 1];
};
const root = createRoot(document.getElementById('root'));
const render = async (element) => {
  await act(async () => {
    root.render(element);
  });
};

// ---------- AssetExportButton ----------

test('导出成功（桌面保存）：请求参数、契约文件名与 success Toast', async () => {
  await clearToasts();
  const requests = [];
  const fetches = [];
  const saved = [];
  const deps = {
    request: async (method, params) => {
      requests.push({ method, params });
      return {
        download_url: '/file-api/download?token=t1',
        filename: 'expert.a.zip',
        size_bytes: 3,
        sha256: 'x',
      };
    },
    fetchImpl: async (url) => {
      fetches.push(url);
      return new Response('pkg');
    },
    saveBlob: async (blob, filename) => {
      saved.push({ size: blob.size, filename });
      return { outcome: 'saved', transport: 'desktop' };
    },
  };
  await render(
    React.createElement(AssetExportButton, {
      kind: 'agent_template',
      localId: 'expert.a',
      deps,
      testId: 'export-btn',
    }),
  );
  await click(find('export-btn'));
  assert.deepEqual(requests, [
    { method: 'assets.export', params: { kind: 'agent_template', local_id: 'expert.a' } },
  ]);
  assert.deepEqual(fetches, ['/file-api/download?token=t1']);
  assert.deepEqual(saved, [{ size: 3, filename: 'expert.a.zip' }]);
  const record = lastToast();
  assert.equal(record.id, 'asset-transfer-export');
  assert.equal(record.content, tt('assetTransfer.messages.exportSavedDesktop'));
  assert.equal(record.variant, 'success');
});

test('导出保存结果四分支：取消 info / 浏览器下载与另存 success / 保存失败 error', async () => {
  const cases = [
    [{ outcome: 'cancelled', transport: 'browser-file-picker' }, 'assetTransfer.messages.exportCancelled', 'info'],
    [{ outcome: 'saved', transport: 'browser-download' }, 'assetTransfer.messages.exportStartedBrowser', 'success'],
    [
      { outcome: 'saved', transport: 'browser-file-picker' },
      'assetTransfer.messages.exportSavedBrowser',
      'success',
    ],
    [{ outcome: 'failed', transport: 'desktop' }, 'assetTransfer.errors.saveFailed', 'error'],
  ];
  for (const [result, expectedKey, expectedVariant] of cases) {
    await clearToasts();
    const deps = {
      request: async () => ({ download_url: '/file-api/download?token=t', filename: 'a.zip' }),
      fetchImpl: async () => new Response('pkg'),
      saveBlob: async () => result,
    };
    await render(
      React.createElement(AssetExportButton, { kind: 'agent_group', localId: 'g', deps, testId: 'export-btn' }),
    );
    await click(find('export-btn'));
    const record = lastToast();
    assert.equal(record.content, tt(expectedKey));
    assert.equal(record.variant, expectedVariant);
  }
});

test('导出失败：服务端契约码映射为对应 Toast（不泄漏原始 message）', async () => {
  await clearToasts();
  const error = Object.assign(new Error('raw server text'), { code: 'ASSET_NOT_FOUND' });
  const deps = {
    request: async () => {
      throw error;
    },
  };
  const results = [];
  await render(
    React.createElement(AssetExportButton, {
      kind: 'agent_template',
      localId: 'missing',
      deps,
      testId: 'export-btn',
      onResult: (r) => results.push(r),
    }),
  );
  await click(find('export-btn'));
  const record = lastToast();
  assert.equal(record.content, tt('assetTransfer.errors.notFound'));
  assert.equal(record.variant, 'error');
  assert.deepEqual(results, []);
});

test('导出进行中：按钮禁用并切换进行态文案，完成后恢复', async () => {
  await clearToasts();
  let resolveRequest;
  const deps = {
    request: () =>
      new Promise((resolve) => {
        resolveRequest = resolve;
      }),
    fetchImpl: async () => new Response('pkg'),
    saveBlob: async () => ({ outcome: 'saved', transport: 'desktop' }),
  };
  await render(
    React.createElement(AssetExportButton, { kind: 'skill', localId: 's', deps, testId: 'export-btn' }),
  );
  const button = find('export-btn');
  await act(async () => {
    button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  });
  await tick();
  assert.equal(button.disabled, true);
  assert.equal(button.getAttribute('aria-busy'), 'true');
  assert.ok(button.textContent.includes(tt('assetTransfer.actions.exporting')));
  // 进行中重复点击不再发起请求（ref 双保险）。
  await click(button);
  await act(async () => {
    resolveRequest({ download_url: '/file-api/download?token=t', filename: 's.zip' });
  });
  await tick();
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, tt('assetTransfer.actions.export'));
});
