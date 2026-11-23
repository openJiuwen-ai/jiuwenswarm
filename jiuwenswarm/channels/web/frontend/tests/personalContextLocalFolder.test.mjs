import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { Simulate } from 'react-dom/test-utils';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';
import { build } from 'esbuild';

const frontend = fileURLToPath(new URL('../', import.meta.url));
const output = new URL('../node_modules/.cache/personal-context-local-folder.mjs', import.meta.url);
await build({
  entryPoints: ['src/components/PersonalContext/AddContentDrawer.tsx'],
  absWorkingDir: frontend,
  outfile: fileURLToPath(output),
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  define: { 'import.meta.env': '{}' },
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  plugins: [{
    name: 'local-folder-boundaries',
    setup(plugin) {
      const mocks = new Map([
        ['../../stores', 'export const usePersonalContextStore = () => globalThis.__pcLocalFolderStore;'],
        ['../../features/settings/settingsNavigation', 'export const requestSettingsModule = () => {};'],
        ['../../components/ui/Toast/toastStore', 'export const toast = { open: (value) => globalThis.__pcLocalFolderToasts.push(value) };'],
      ]);
      plugin.onResolve({ filter: /.*/ }, (args) => {
        if (args.path.endsWith('.svg?react')) return { path: 'icon', namespace: 'local-folder' };
        if (args.path.endsWith('/webClient')) return { path: 'webClient', namespace: 'local-folder' };
        return mocks.has(args.path) ? { path: args.path, namespace: 'local-folder' } : null;
      });
      plugin.onLoad({ filter: /.*/, namespace: 'local-folder' }, (args) => ({
        contents: args.path === 'icon'
          ? 'export default () => null;'
          : args.path === 'webClient'
            ? 'export const webClient = {}; export const webRequest = (...args) => globalThis.__pcLocalFolderRequest(...args);'
            : mocks.get(args.path),
      }));
    },
  }],
});
const { AddContentDrawer } = await import(output);

async function withDrawer({ language = 'zh', edit = false, provider = 'local_files', hostPlatform = 'win32', browserPlatform = 'Win32', picker, filePicker, bookmarksPath, request } = {}, run) {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://remote-server.example' });
  Object.defineProperty(dom.window.navigator, 'platform', { value: browserPlatform });
  const globals = {
    window: dom.window,
    document: dom.window.document,
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
  };
  const originals = new Map(Object.keys(globals).map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  for (const [key, value] of Object.entries(globals)) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  if (picker !== undefined || filePicker !== undefined) {
    dom.window.pywebview = { api: { select_project_directory: picker, select_local_files: filePicker } };
  }
  const submissions = [];
  const requests = [];
  const toasts = [];
  let completed = 0;
  globalThis.__pcLocalFolderStore = {
    config: { master_enabled: true, collection_enabled: true, ...(hostPlatform ? { host_platform: hostPlatform } : {}) },
    createService: async (config) => submissions.push({ operation: 'create', config }),
    updateService: async (serviceId, config) => submissions.push({ operation: 'update', serviceId, config }),
    pendingWrites: {},
    isProviderAuthorized: () => true,
  };
  globalThis.__pcLocalFolderToasts = toasts;
  globalThis.__pcLocalFolderRequest = async (...args) => {
    requests.push(args);
    if (request) return request(...args);
    throw Object.assign(new Error('system directory picker is unavailable'), { code: 'UNSUPPORTED' });
  };
  const locale = JSON.parse(await readFile(new URL(`../src/i18n/locales/${language}.json`, import.meta.url), 'utf8'));
  const i18n = createInstance();
  await i18n.init({ lng: language, resources: { [language]: { translation: locale } } });
  const root = createRoot(dom.window.document.getElementById('root'));
  const get = (id) => dom.window.document.querySelector(`[data-testid="${id}"]`);
  const change = async (id, value) => act(async () => Simulate.change(get(id), { target: { value } }));
  try {
    await act(async () => root.render(createElement(I18nextProvider, { i18n }, createElement(AddContentDrawer, {
      initialProvider: provider,
      editService: edit ? {
        service_id: 'notes', provider, source: provider === 'local_files' ? { root_dir: '/srv/old-notes' } : {
          profile: 'Profile 1', ...(bookmarksPath !== undefined ? { bookmarks_path: bookmarksPath } : {}),
        },
        interval_seconds: 86400, max_items_per_run: 20,
      } : null,
      onClose() {},
      onCreated() { completed += 1; },
    }))));
    await run({ get, change, submissions, requests, toasts, locale });
    assert.equal(completed, submissions.length);
  } finally {
    await act(async () => root.unmount());
    delete globalThis.__pcLocalFolderStore;
    delete globalThis.__pcLocalFolderToasts;
    delete globalThis.__pcLocalFolderRequest;
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
    dom.window.close();
  }
}

const edgeSourceDefaults = { include_subfolders: true, fetch_page_content: true };
const edgeProfilePicker = (get) => get('personal-context-edge-profile').closest('.pc-drawer__field').querySelector('button');
const edgeFilePicker = (get) => get('personal-context-edge-bookmarks-path').closest('.pc-drawer__field').querySelector('button');
const bookmarksPicks = (path) => [{ path, filename: 'Bookmarks', size: 12, mime_type: 'application/json', kind: 'document' }];

for (const hostPlatform of ['linux', 'darwin', '', 'freebsd', 'win32']) {
  for (const edit of [false, true]) {
    test(`Edge file path on ${hostPlatform || 'missing'} Web can ${edit ? 'edit' : 'create'} using a manual host path`, async () => {
      await withDrawer({ provider: 'browser_bookmarks', edit, hostPlatform }, async ({ get, change, submissions, requests }) => {
        assert.equal(Boolean(edgeFilePicker(get)), hostPlatform === 'win32');
        assert.equal(get('personal-context-edge-bookmarks-path').readOnly, false);
        if (!edit) await change('personal-context-content-name', 'notes');
        await change('personal-context-edge-profile', 'Profile 2');
        await change('personal-context-edge-bookmarks-path', '  /srv/浏览器 用户/Bookmarks  ');
        await act(async () => get('personal-context-content-submit').click());
        assert.equal(submissions[0].operation, edit ? 'update' : 'create');
        assert.deepEqual(submissions[0].config.source, { ...edgeSourceDefaults, profile: 'Profile 2', bookmarks_path: '/srv/浏览器 用户/Bookmarks' });
        assert.equal(requests.length, 0);
      });
    });
  }
}

for (const edit of [false, true]) {
  test(`English Edge file path can ${edit ? 'edit' : 'create'} on Linux without a file picker`, async () => {
    await withDrawer({ language: 'en', provider: 'browser_bookmarks', edit, hostPlatform: 'linux' }, async ({ get, change, submissions }) => {
      assert.equal(Boolean(edgeFilePicker(get)), false);
      assert.equal(get('personal-context-edge-bookmarks-path').readOnly, false);
      if (!edit) await change('personal-context-content-name', 'notes');
      await change('personal-context-edge-profile', '');
      await change('personal-context-edge-bookmarks-path', '/srv/edge/Bookmarks');
      await act(async () => get('personal-context-content-submit').click());
      assert.deepEqual(submissions[0].config.source, { ...edgeSourceDefaults, bookmarks_path: '/srv/edge/Bookmarks' });
    });
  });
}

for (const hostPlatform of ['win32', 'darwin', 'linux', '']) {
  for (const edit of [false, true]) {
    test(`Edge native file-only bridge can ${edit ? 'edit' : 'create'} on ${hostPlatform || 'missing'} without a directory bridge`, async () => {
      const path = hostPlatform === 'win32' ? 'C:\\Edge\\Profile 2\\Bookmarks' : '/home/user/edge/Profile 2/Bookmarks';
      const calls = [];
      await withDrawer({ provider: 'browser_bookmarks', edit, hostPlatform, filePicker: async (...args) => { calls.push(args); return bookmarksPicks(path); } }, async ({ get, change, submissions, requests }) => {
        const browse = edgeFilePicker(get);
        assert.ok(browse);
        assert.equal(Boolean(edgeProfilePicker(get)), hostPlatform === 'win32');
        if (!edit) await change('personal-context-content-name', 'notes');
        await change('personal-context-edge-profile', 'Profile 2');
        await act(async () => browse.click());
        assert.deepEqual(calls, [[false]]);
        assert.equal(get('personal-context-edge-bookmarks-path').value, path);
        await act(async () => get('personal-context-content-submit').click());
        assert.deepEqual(submissions[0].config.source, { ...edgeSourceDefaults, profile: 'Profile 2', bookmarks_path: path });
        assert.equal(requests.length, 0);
      });
    });
  }
}

for (const filePicker of [undefined, 'not a function']) {
  test(`Edge Linux directory-only bridge does not advertise a ${filePicker ? 'non-function' : 'missing'} file picker`, async () => {
    await withDrawer({ provider: 'browser_bookmarks', hostPlatform: 'linux', picker: async () => '/home/user/Profile 2', filePicker }, async ({ get, requests }) => {
      assert.ok(edgeProfilePicker(get));
      assert.equal(Boolean(edgeFilePicker(get)), false);
      assert.equal(get('personal-context-edge-bookmarks-path').readOnly, false);
      assert.equal(requests.length, 0);
    });
  });
}

for (const edit of [false, true]) {
  test(`Edge Windows Web ${edit ? 'edit' : 'create'} retains its file backend channel with a non-function bridge`, async () => {
    const path = 'C:\\Edge\\Bookmarks';
    await withDrawer({ provider: 'browser_bookmarks', edit, browserPlatform: 'Linux x86_64', filePicker: 'not a function', request: async () => ({ files: bookmarksPicks(path) }) }, async ({ get, change, submissions, requests }) => {
      assert.ok(edgeFilePicker(get));
      if (!edit) await change('personal-context-content-name', 'notes');
      await change('personal-context-edge-profile', '');
      await act(async () => edgeFilePicker(get).click());
      assert.equal(requests[0][0], 'path.select_files');
      assert.deepEqual(requests[0][1], { allow_multiple: false });
      await act(async () => get('personal-context-content-submit').click());
      assert.deepEqual(submissions[0].config.source, { ...edgeSourceDefaults, bookmarks_path: path });
    });
  });
}

for (const channel of ['web', 'desktop']) {
  for (const failure of [false, true]) {
    test(`Edge ${channel} file ${failure ? 'failure' : 'cancellation'} preserves the saved path and profile`, async () => {
      const path = '/srv/saved/Bookmarks';
      const pick = async () => {
        if (failure) throw new Error('file picker failed');
        return [];
      };
      const options = channel === 'desktop' ? { filePicker: pick } : { request: async () => ({ files: await pick() }) };
      await withDrawer({ provider: 'browser_bookmarks', edit: true, bookmarksPath: path, ...options }, async ({ get, submissions, toasts }) => {
        assert.equal(get('personal-context-edge-bookmarks-path').value, path);
        await act(async () => edgeFilePicker(get).click());
        assert.equal(get('personal-context-edge-bookmarks-path').value, path);
        assert.equal(toasts.length, failure ? 1 : 0);
        await act(async () => get('personal-context-content-submit').click());
        assert.deepEqual(submissions[0].config.source, { ...edgeSourceDefaults, profile: 'Profile 1', bookmarks_path: path });
      });
    });
  }
}

for (const language of ['zh', 'en']) {
  test(`${language} Edge can clear a saved file path to restore the default without changing the profile`, async () => {
    await withDrawer({ language, provider: 'browser_bookmarks', edit: true, hostPlatform: 'linux', bookmarksPath: '/srv/old/Bookmarks' }, async ({ get, change, submissions }) => {
      assert.equal(get('personal-context-edge-bookmarks-path').value, '/srv/old/Bookmarks');
      assert.equal(get('personal-context-edge-bookmarks-path').readOnly, false);
      await change('personal-context-edge-bookmarks-path', '   ');
      await act(async () => get('personal-context-content-submit').click());
      assert.deepEqual(submissions[0].config.source, { ...edgeSourceDefaults, profile: 'Profile 1' });
    });
  });
}

for (const edit of [false, true]) {
  for (const manualPath of ['/srv/manual/Bookmarks', '/srv/old/Bookmarks', '']) {
    test(`Edge ${edit ? 'edit' : 'create'} keeps a ${manualPath ? 'newer' : 'cleared'} manual file path after a delayed native pick`, async () => {
      let finish;
      const selected = new Promise((resolve) => { finish = resolve; });
      await withDrawer({ provider: 'browser_bookmarks', edit, bookmarksPath: '/srv/old/Bookmarks', filePicker: () => selected }, async ({ get, change, submissions, toasts }) => {
        if (!edit) await change('personal-context-content-name', 'notes');
        await change('personal-context-edge-profile', '');
        await act(async () => edgeFilePicker(get).click());
        await change('personal-context-edge-bookmarks-path', '/srv/intermediate/Bookmarks');
        await change('personal-context-edge-bookmarks-path', manualPath);
        await act(async () => finish(bookmarksPicks('/srv/picked/Bookmarks')));
        assert.equal(get('personal-context-edge-bookmarks-path').value, manualPath);
        assert.equal(toasts.length, 0);
        await act(async () => get('personal-context-content-submit').click());
        assert.deepEqual(submissions[0].config.source, { ...edgeSourceDefaults, ...(manualPath ? { bookmarks_path: manualPath } : {}) });
      });
    });
  }
}

for (const hostPlatform of ['linux', 'win32']) {
  test(`Edge ${hostPlatform} treats a non-function native bridge as unavailable`, async () => {
    await withDrawer({ provider: 'browser_bookmarks', hostPlatform, picker: 'not a function', request: async () => ({ cancelled: true }) }, async ({ get, requests }) => {
      const browse = edgeProfilePicker(get);
      assert.equal(Boolean(browse), hostPlatform === 'win32');
      if (browse) await act(async () => browse.click());
      assert.equal(requests.length, hostPlatform === 'win32' ? 1 : 0);
    });
  });
}

test('Edge native directory selection keeps UNC profile names and the adjacent file field', async () => {
  await withDrawer({ provider: 'browser_bookmarks', picker: async () => '\\\\server\\share\\Profile 2' }, async ({ get, change, submissions }) => {
    await change('personal-context-content-name', 'notes');
    await act(async () => edgeProfilePicker(get).click());
    assert.equal(get('personal-context-edge-profile').value, 'Profile 2');
    const bookmarks = get('personal-context-edge-bookmarks-path');
    assert.equal(bookmarks.readOnly, false);
    assert.equal(bookmarks.value, '');
    assert.ok(bookmarks.closest('.pc-drawer__field').querySelector('button'));
    await act(async () => get('personal-context-content-submit').click());
    assert.deepEqual(submissions[0].config.source, { ...edgeSourceDefaults, profile: 'Profile 2' });
  });
});

test('Edge Windows Web unsupported folder selection preserves manual entry and feedback', async () => {
  await withDrawer({ provider: 'browser_bookmarks', edit: true }, async ({ get, change, submissions, toasts }) => {
    await change('personal-context-edge-profile', 'Profile manual');
    await act(async () => edgeProfilePicker(get).click());
    assert.equal(toasts.length, 1);
    assert.equal(toasts[0].variant, 'error');
    assert.equal(get('personal-context-edge-profile').value, 'Profile manual');
    await act(async () => get('personal-context-content-submit').click());
    assert.deepEqual(submissions[0].config.source, { ...edgeSourceDefaults, profile: 'Profile manual' });
  });
});

for (const language of ['zh', 'en']) {
  for (const edit of [false, true]) {
    test(`${language} Edge ${edit ? 'edit' : 'create'} on Linux Web hides the folder picker and submits a manual profile`, async () => {
      await withDrawer({ language, edit, provider: 'browser_bookmarks', hostPlatform: 'linux' }, async ({ get, change, submissions, requests }) => {
        assert.equal(Boolean(edgeProfilePicker(get)), false);
        assert.equal(get('personal-context-edge-profile').readOnly, false);
        if (!edit) await change('personal-context-content-name', 'notes');
        await change('personal-context-edge-profile', '  Profile 2  ');
        await act(async () => get('personal-context-content-submit').click());
        assert.equal(submissions[0].operation, edit ? 'update' : 'create');
        assert.deepEqual(submissions[0].config.source, { ...edgeSourceDefaults, profile: 'Profile 2' });
        assert.equal(requests.length, 0);
      });
    });
  }
}

for (const hostPlatform of ['darwin', '', 'freebsd']) {
  test(`Edge host ${hostPlatform || 'missing'} without a native bridge hides the folder picker and preserves the default profile`, async () => {
    await withDrawer({ provider: 'browser_bookmarks', hostPlatform }, async ({ get, change, submissions, requests }) => {
      assert.equal(Boolean(edgeProfilePicker(get)), false);
      await change('personal-context-content-name', 'notes');
      await act(async () => get('personal-context-content-submit').click());
      assert.deepEqual(submissions[0].config.source, edgeSourceDefaults);
      assert.equal(requests.length, 0);
    });
  });
}

for (const hostPlatform of ['win32', 'darwin', 'linux', '']) {
  for (const edit of [false, true]) {
    test(`Edge native folder picker can ${edit ? 'edit' : 'create'} on host ${hostPlatform || 'missing'} and stores only the profile name`, async () => {
      let picks = 0;
      const path = hostPlatform === 'win32' ? 'D:\\Edge\\User Data\\Profile 2' : '/home/user/edge/Profile 2/';
      await withDrawer({ provider: 'browser_bookmarks', edit, hostPlatform, picker: async () => { picks += 1; return path; } }, async ({ get, change, submissions, requests }) => {
        if (!edit) await change('personal-context-content-name', 'notes');
        const browse = edgeProfilePicker(get);
        assert.ok(browse);
        await act(async () => browse.click());
        assert.equal(picks, 1);
        assert.equal(get('personal-context-edge-profile').value, 'Profile 2');
        await act(async () => get('personal-context-content-submit').click());
        assert.deepEqual(submissions[0].config.source, { ...edgeSourceDefaults, profile: 'Profile 2' });
        assert.equal(submissions[0].operation, edit ? 'update' : 'create');
        assert.equal(requests.length, 0);
      });
    });
  }
}

for (const edit of [false, true]) {
  test(`Edge Windows host ${edit ? 'edit' : 'create'} retains the backend folder channel with a Linux browser`, async () => {
    await withDrawer({ provider: 'browser_bookmarks', edit, browserPlatform: 'Linux x86_64', request: async () => ({ path: 'C:\\Edge\\Profile 3' }) }, async ({ get, change, submissions, requests }) => {
      if (!edit) await change('personal-context-content-name', 'notes');
      const browse = edgeProfilePicker(get);
      assert.ok(browse);
      await act(async () => browse.click());
      assert.equal(requests.length, 1);
      assert.equal(requests[0][0], 'path.select_directory');
      assert.deepEqual(requests[0][1], {});
      await act(async () => get('personal-context-content-submit').click());
      assert.deepEqual(submissions[0].config.source, { ...edgeSourceDefaults, profile: 'Profile 3' });
    });
  });
}

for (const channel of ['web', 'desktop']) {
  for (const failure of [false, true]) {
    test(`Edge ${channel} folder picker ${failure ? 'failure' : 'cancellation'} preserves manual entry`, async () => {
      const pick = async () => {
        if (failure) throw new Error('picker failed');
        return null;
      };
      const options = channel === 'desktop' ? { picker: pick } : { request: async () => { await pick(); return { cancelled: true }; } };
      await withDrawer({ provider: 'browser_bookmarks', edit: true, ...options }, async ({ get, change, submissions, toasts }) => {
        await change('personal-context-edge-profile', 'Profile manual');
        await act(async () => edgeProfilePicker(get).click());
        assert.equal(get('personal-context-edge-profile').value, 'Profile manual');
        assert.equal(toasts.length, failure ? 1 : 0);
        if (failure) assert.equal(toasts[0].variant, 'error');
        await act(async () => get('personal-context-content-submit').click());
        assert.deepEqual(submissions[0].config.source, { ...edgeSourceDefaults, profile: 'Profile manual' });
      });
    });
  }
}

for (const edit of [false, true]) {
  test(`Edge ${edit ? 'edit' : 'create'} can clear the profile to use the default`, async () => {
    await withDrawer({ provider: 'browser_bookmarks', edit, hostPlatform: 'linux' }, async ({ get, change, submissions }) => {
      if (!edit) await change('personal-context-content-name', 'notes');
      await change('personal-context-edge-profile', '   ');
      await act(async () => get('personal-context-content-submit').click());
      assert.deepEqual(submissions[0].config.source, edgeSourceDefaults);
    });
  });
}

for (const language of ['zh', 'en']) {
  for (const edit of [false, true]) {
    test(`${language} remote Web can ${edit ? 'edit' : 'create'} a local-folder source using a server path`, async () => {
      await withDrawer({ language, edit, hostPlatform: 'linux' }, async ({ get, change, submissions, requests, locale }) => {
        const path = get('personal-context-local-root-directory');
        assert.equal(path.readOnly, false);
        assert.equal(Boolean(get('personal-context-local-root-directory-browse')), false);
        assert.equal(get('personal-context-local-root-directory-hint').textContent, locale.personalContext.addContent.localFiles.rootDirHint);
        assert.ok(path.getAttribute('aria-describedby').split(' ').includes('personal-context-local-root-directory-hint'));
        if (!edit) await change('personal-context-content-name', 'notes');
        await change('personal-context-local-root-directory', '  /srv/团队 笔记  ');
        await act(async () => get('personal-context-content-submit').click());
        assert.equal(submissions.length, 1);
        assert.equal(submissions[0].operation, edit ? 'update' : 'create');
        assert.deepEqual(submissions[0].config.source, { root_dir: '/srv/团队 笔记' });
        assert.equal(requests.length, 0);
      });
    });
  }
}

for (const hostPlatform of ['linux', 'darwin', '', 'freebsd']) {
  test(`host platform ${hostPlatform || 'missing'} hides the picker without a native bridge even with a Windows browser`, async () => {
    await withDrawer({ hostPlatform }, async ({ get, change, submissions, requests }) => {
      assert.equal(Boolean(get('personal-context-local-root-directory-browse')), false);
      assert.equal(get('personal-context-local-root-directory').readOnly, false);
      await change('personal-context-content-name', 'notes');
      await change('personal-context-local-root-directory', '/home/notes');
      await act(async () => get('personal-context-content-submit').click());
      assert.deepEqual(submissions[0].config.source, { root_dir: '/home/notes' });
      assert.equal(requests.length, 0);
    });
  });
}

test('remote Web rejects an empty server path before creating a source', async () => {
  await withDrawer({}, async ({ get, change, submissions }) => {
    await change('personal-context-content-name', 'notes');
    await change('personal-context-local-root-directory', '   ');
    const submit = get('personal-context-content-submit');
    assert.equal(submit.disabled, true);
    await act(async () => submit.click());
    assert.equal(submissions.length, 0);
  });
});

test('Windows host keeps the backend picker even when the browser reports Linux', async () => {
  await withDrawer({ browserPlatform: 'Linux x86_64', request: async () => ({ path: '/srv/团队 笔记', cancelled: false }) }, async ({ get, change, submissions, requests }) => {
    await change('personal-context-content-name', 'notes');
    await change('personal-context-local-root-directory', '/srv/start');
    const browse = get('personal-context-local-root-directory-browse');
    assert.ok(browse);
    await act(async () => browse.click());
    assert.equal(requests.length, 1);
    assert.equal(requests[0][0], 'path.select_directory');
    assert.deepEqual(requests[0][1], { initial_dir: '/srv/start' });
    assert.equal(get('personal-context-local-root-directory').value, '/srv/团队 笔记');
    await act(async () => get('personal-context-content-submit').click());
    assert.deepEqual(submissions[0].config.source, { root_dir: '/srv/团队 笔记' });
  });
});

for (const language of ['zh', 'en']) {
  test(`${language} unsupported Web picker explains manual entry and keeps the form usable`, async () => {
    await withDrawer({ language }, async ({ get, change, submissions, requests, toasts, locale }) => {
      await change('personal-context-content-name', 'notes');
      const browse = get('personal-context-local-root-directory-browse');
      assert.ok(browse);
      await act(async () => browse.click());
      assert.deepEqual(requests[0][1], {});
      assert.equal(toasts.length, 1);
      assert.equal(toasts[0].variant, 'info');
      assert.equal(toasts[0].content, locale.personalContext.addContent.localFiles.rootDirHint);
      await change('personal-context-local-root-directory', '/srv/manual-notes');
      await act(async () => get('personal-context-content-submit').click());
      assert.deepEqual(submissions[0].config.source, { root_dir: '/srv/manual-notes' });
    });
  });
}

test('cancelling the Web picker preserves the entered path without feedback', async () => {
  await withDrawer({ request: async () => ({ path: null, cancelled: true }) }, async ({ get, change, requests, toasts }) => {
    await change('personal-context-local-root-directory', '/srv/manual-notes');
    const browse = get('personal-context-local-root-directory-browse');
    assert.ok(browse);
    await act(async () => browse.click());
    assert.equal(requests.length, 1);
    assert.equal(get('personal-context-local-root-directory').value, '/srv/manual-notes');
    assert.equal(toasts.length, 0);
  });
});

test('Web picker failure preserves the path and allows a corrected manual submission', async () => {
  await withDrawer({ request: async () => { throw new Error('backend picker failed'); } }, async ({ get, change, submissions, requests, toasts }) => {
    await change('personal-context-content-name', 'notes');
    await change('personal-context-local-root-directory', '/srv/old-notes');
    const browse = get('personal-context-local-root-directory-browse');
    assert.ok(browse);
    await act(async () => browse.click());
    assert.equal(requests.length, 1);
    assert.equal(get('personal-context-local-root-directory').value, '/srv/old-notes');
    assert.equal(toasts.length, 1);
    assert.equal(toasts[0].variant, 'error');
    assert.equal(toasts[0].content, 'backend picker failed');
    await change('personal-context-local-root-directory', '/srv/corrected-notes');
    await act(async () => get('personal-context-content-submit').click());
    assert.deepEqual(submissions[0].config.source, { root_dir: '/srv/corrected-notes' });
  });
});

for (const hostPlatform of ['win32', 'darwin', 'linux', '']) {
  for (const edit of [false, true]) {
    test(`native bridge can ${edit ? 'edit' : 'create'} a local-folder source on host platform ${hostPlatform || 'missing'}`, async () => {
      let picks = 0;
      const selectedPath = hostPlatform === 'win32' ? 'D:\\团队 笔记' : '/home/团队 笔记';
      await withDrawer({ hostPlatform, edit, picker: async () => { picks += 1; return selectedPath; } }, async ({ get, change, submissions, requests, locale }) => {
        if (!edit) await change('personal-context-content-name', 'notes');
        const browse = get('personal-context-local-root-directory-browse');
        assert.ok(browse, 'the native directory picker must remain available');
        assert.equal(browse.getAttribute('aria-label'), locale.personalContext.addContent.localFiles.browse);
        await act(async () => browse.click());
        assert.equal(picks, 1);
        assert.equal(get('personal-context-local-root-directory').value, selectedPath);
        await act(async () => get('personal-context-content-submit').click());
        assert.equal(submissions[0].operation, edit ? 'update' : 'create');
        assert.deepEqual(submissions[0].config.source, { root_dir: selectedPath });
        assert.equal(requests.length, 0);
      });
    });
  }
}

test('cancelling the desktop picker preserves a manually entered path', async () => {
  await withDrawer({ picker: async () => null }, async ({ get, change, toasts }) => {
    await change('personal-context-local-root-directory', '/srv/manual-notes');
    await act(async () => get('personal-context-local-root-directory-browse').click());
    assert.equal(get('personal-context-local-root-directory').value, '/srv/manual-notes');
    assert.equal(toasts.length, 0);
  });
});

test('desktop picker failure provides feedback while manual entry can still be submitted', async () => {
  await withDrawer({ picker: async () => { throw new Error('native picker failed'); } }, async ({ get, change, submissions, toasts }) => {
    await change('personal-context-content-name', 'notes');
    await act(async () => get('personal-context-local-root-directory-browse').click());
    assert.equal(toasts.length, 1);
    assert.equal(toasts[0].variant, 'error');
    await change('personal-context-local-root-directory', '/srv/manual-notes');
    await act(async () => get('personal-context-content-submit').click());
    assert.deepEqual(submissions[0].config.source, { root_dir: '/srv/manual-notes' });
  });
});

for (const channel of ['web', 'desktop']) {
  for (const edit of [false, true]) {
    test(`${channel} ${edit ? 'edit' : 'create'} preserves newer manual input after a delayed picker success`, async () => {
      let completePick;
      const response = new Promise((resolve) => { completePick = resolve; });
      const options = channel === 'web' ? { edit, request: () => response } : { edit, picker: () => response };
      const initialPath = channel === 'web' ? '/srv/old-notes' : 'D:\\旧 笔记';
      const manualPath = channel === 'web' ? '/srv/新 笔记' : '\\\\server\\share\\新 笔记';
      await withDrawer(options, async ({ get, change, submissions }) => {
        if (!edit) await change('personal-context-content-name', 'notes');
        await change('personal-context-local-root-directory', initialPath);
        await act(async () => get('personal-context-local-root-directory-browse').click());
        await change('personal-context-local-root-directory', manualPath);
        await act(async () => completePick(channel === 'web' ? { path: initialPath } : initialPath));
        assert.equal(get('personal-context-local-root-directory').value, manualPath);
        await act(async () => get('personal-context-content-submit').click());
        assert.equal(submissions[0].config.source.root_dir, manualPath);
      });
    });
  }
}

test('a manual edit restored to its original value still supersedes a pending picker', async () => {
  let completePick;
  const response = new Promise((resolve) => { completePick = resolve; });
  await withDrawer({ request: () => response }, async ({ get, change }) => {
    await change('personal-context-local-root-directory', '/srv/original');
    await act(async () => get('personal-context-local-root-directory-browse').click());
    await change('personal-context-local-root-directory', '/srv/new-manual');
    await change('personal-context-local-root-directory', '/srv/original');
    await act(async () => completePick({ path: '/srv/picked' }));
    assert.equal(get('personal-context-local-root-directory').value, '/srv/original');
  });
});

test('pending picker successes can still update the path when there is no newer manual edit', async () => {
  const responses = [];
  await withDrawer({ request: () => new Promise((resolve) => responses.push(resolve)) }, async ({ get, change }) => {
    await change('personal-context-local-root-directory', '/srv/original');
    await act(async () => get('personal-context-local-root-directory-browse').click());
    await act(async () => get('personal-context-local-root-directory-browse').click());
    assert.equal(responses.length, 2);
    await act(async () => responses[0]({ path: '/srv/first-pick' }));
    assert.equal(get('personal-context-local-root-directory').value, '/srv/first-pick');
    await act(async () => responses[1]({ path: '/srv/second-pick' }));
    assert.equal(get('personal-context-local-root-directory').value, '/srv/second-pick');
  });
});
