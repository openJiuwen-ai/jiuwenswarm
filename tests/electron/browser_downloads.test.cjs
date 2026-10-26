const assert = require('node:assert/strict');
const { test } = require('node:test');
const { EventEmitter } = require('node:events');
const { createBrowserDownloadService, MAX_DOWNLOAD_RECORDS } = require('../../jiuwenswarm/channels/desktop/electron/browser_downloads.cjs');

function createDownloadItem(overrides = {}) {
  const item = new EventEmitter();
  let paused = false;
  let cancelled = false;
  item.filename = overrides.filename || 'setup.exe';
  item.url = overrides.url || 'https://example.com/setup.exe';
  item.savePath = overrides.savePath || 'C:\\Downloads\\setup.exe';
  item.receivedBytes = overrides.receivedBytes || 0;
  item.totalBytes = overrides.totalBytes ?? 383 * 1024 * 1024;
  item.state = overrides.state || 'progressing';
  item.getFilename = () => item.filename;
  item.getURL = () => item.url;
  item.getSavePath = () => item.savePath;
  item.setSavePath = value => { item.savePath = value; };
  item.getReceivedBytes = () => item.receivedBytes;
  item.getTotalBytes = () => item.totalBytes;
  item.isPaused = () => paused;
  item.canResume = () => item.state === 'progressing';
  item.pause = () => { paused = true; };
  item.resume = () => { paused = false; };
  item.cancel = () => {
    cancelled = true;
    item.state = 'cancelled';
    item.emit('done', {}, 'cancelled');
  };
  item.getState = () => item.state;
  item.isCancelled = () => cancelled;
  return item;
}

function openPathsOf(serviceCalls) {
  return serviceCalls;
}

test('will-download records progress and completion with coalesced notifications', async () => {
  const notifications = [];
  const session = new EventEmitter();
  const service = createBrowserDownloadService({
    openPath: async () => '',
    getDownloadsPath: () => 'C:\\Downloads',
    notifyCoalesceMs: 1,
  });
  service.attach(session, list => notifications.push(list));

  const item = createDownloadItem();
  session.emit('will-download', {}, item);
  item.receivedBytes = 7.3 * 1024 * 1024;
  item.emit('updated', {}, item);
  await new Promise(resolve => setTimeout(resolve, 10));

  item.state = 'completed';
  item.receivedBytes = item.totalBytes;
  item.emit('done', {}, 'completed');
  await new Promise(resolve => setTimeout(resolve, 10));

  const list = service.list();
  assert.equal(list.length, 1);
  assert.equal(list[0].filename, 'setup.exe');
  assert.equal(list[0].state, 'completed');
  assert.equal(list[0].receivedBytes, item.totalBytes);
  assert.ok(list[0].endTime >= list[0].startTime);
  // Progress events coalesce into at most a couple of notifications, not one per chunk.
  assert.ok(notifications.length <= 3, `expected coalesced notifications, got ${notifications.length}`);
});

test('pause, resume and cancel actions drive the underlying DownloadItem', async () => {
  const session = new EventEmitter();
  const service = createBrowserDownloadService({
    openPath: async () => '',
    getDownloadsPath: () => 'C:\\Downloads',
    notifyCoalesceMs: 1,
  });
  service.attach(session, () => {});

  const item = createDownloadItem();
  session.emit('will-download', {}, item);
  const [record] = service.list();

  assert.equal(service.action(record.id, 'pause'), true);
  item.emit('updated', {}, item);
  assert.equal(service.list()[0].state, 'paused');

  assert.equal(service.action(record.id, 'resume'), true);
  item.emit('updated', {}, item);
  assert.equal(service.list()[0].state, 'downloading');

  assert.equal(service.action(record.id, 'cancel'), true);
  assert.equal(service.list()[0].state, 'cancelled');
  assert.equal(item.isCancelled(), true);
  assert.equal(service.action(record.id, 'pause'), false);
});

test('attach is idempotent: a second call must not duplicate records', async () => {
  const session = new EventEmitter();
  const service = createBrowserDownloadService({
    openPath: async () => '',
    getDownloadsPath: () => 'C:\\Downloads',
    notifyCoalesceMs: 1,
  });
  service.attach(session, () => {});
  service.attach(session, () => {});

  session.emit('will-download', {}, createDownloadItem());
  assert.equal(service.list().length, 1);
});

test('revealInFolder only reveals completed downloads with a path', async () => {
  const revealed = [];
  const session = new EventEmitter();
  const service = createBrowserDownloadService({
    openPath: async () => '',
    getDownloadsPath: () => 'C:\\Downloads',
    showInFolder: filePath => {
      revealed.push(filePath);
    },
    notifyCoalesceMs: 1,
  });
  service.attach(session, () => {});

  // A completed download can be revealed in its folder.
  const done = createDownloadItem({ savePath: 'C:\\Downloads\\done.exe' });
  session.emit('will-download', {}, done);
  const [doneRecord] = service.list();
  done.state = 'completed';
  done.emit('done', {}, 'completed');
  assert.equal(service.revealInFolder(doneRecord.id), true);
  assert.deepEqual(revealed, ['C:\\Downloads\\done.exe']);

  // A still-downloading record must not be revealed.
  const active = createDownloadItem({ savePath: 'C:\\Downloads\\active.exe' });
  session.emit('will-download', {}, active);
  const [activeRecord] = service.list();
  assert.equal(activeRecord.state, 'downloading');
  assert.equal(service.revealInFolder(activeRecord.id), false);
  assert.equal(service.revealInFolder('missing-id'), false);
});

test('download history restores from storage, marks unfinished interrupted and saves changes', async () => {
  const saved = [];
  const storage = {
    load: () => [
      {
        id: 'download-7',
        filename: 'old.exe',
        url: 'https://example.com/old.exe',
        path: 'C:\\Downloads\\old.exe',
        receivedBytes: 10,
        totalBytes: 10,
        state: 'completed',
        startTime: 1,
        endTime: 2,
      },
      {
        id: 'download-8',
        filename: 'mid.zip',
        url: 'https://example.com/mid.zip',
        path: 'C:\\Downloads\\mid.zip',
        receivedBytes: 5,
        totalBytes: 50,
        state: 'paused',
        startTime: 3,
        endTime: 0,
      },
      { id: 'bogus', filename: 'x.txt', state: 'not-a-state' },
    ],
    save: records => saved.push(records),
  };
  const session = new EventEmitter();
  const service = createBrowserDownloadService({
    openPath: async () => '',
    getDownloadsPath: () => 'C:\\Downloads',
    storage,
    notifyCoalesceMs: 1,
  });
  service.attach(session, () => {});

  // Invalid entries are dropped; live DownloadItems cannot survive a restart
  // so unfinished records come back as interrupted (Chrome marks them the same).
  const restored = service.list();
  assert.equal(restored.length, 2);
  assert.equal(restored[0].state, 'completed');
  assert.equal(restored[1].state, 'interrupted');

  // New records continue the restored id sequence.
  const item = createDownloadItem({ filename: 'new.exe', savePath: 'C:\\Downloads\\new.exe' });
  session.emit('will-download', {}, item);
  item.state = 'completed';
  item.receivedBytes = item.totalBytes;
  item.emit('done', {}, 'completed');
  await new Promise(resolve => setTimeout(resolve, 10));

  assert.ok(saved.length > 0);
  const snapshot = saved[saved.length - 1];
  assert.deepEqual(
    snapshot.map(record => record.id),
    ['download-9', 'download-7', 'download-8'],
  );
  // Icons are runtime-only data URLs; they must never hit the disk.
  for (const record of snapshot) {
    assert.equal('icon' in record, false);
  }
});

test('completed downloads whose file vanished are flagged and refuse open/reveal', async () => {
  const calls = { open: 0, reveal: 0 };
  // Simulate the disk: kept.exe lands on completion, moved.exe never does.
  const disk = new Set();
  const session = new EventEmitter();
  const service = createBrowserDownloadService({
    openPath: async () => {
      calls.open += 1;
      return '';
    },
    getDownloadsPath: () => 'C:\\Downloads',
    showInFolder: () => {
      calls.reveal += 1;
    },
    pathExists: filePath => disk.has(filePath),
    notifyCoalesceMs: 1,
  });
  service.attach(session, () => {});

  const kept = createDownloadItem({ filename: 'kept.exe', savePath: 'C:\\Downloads\\kept.exe' });
  session.emit('will-download', {}, kept);
  kept.state = 'completed';
  kept.emit('done', {}, 'completed');
  disk.add('C:\\Downloads\\kept.exe');
  const moved = createDownloadItem({ filename: 'moved.exe', savePath: 'C:\\Downloads\\moved.exe' });
  session.emit('will-download', {}, moved);
  moved.state = 'completed';
  moved.emit('done', {}, 'completed');

  // Newest first: moved was recorded last.
  const [movedRecord, keptRecord] = service.list();
  assert.equal(keptRecord.fileMissing, false);
  assert.equal(movedRecord.fileMissing, true);

  assert.equal(await service.openDownload(keptRecord.id), true);
  assert.equal(await service.openDownload(movedRecord.id), false);
  assert.equal(service.revealInFolder(keptRecord.id), true);
  assert.equal(service.revealInFolder(movedRecord.id), false);
  // Exactly one OS call each: vanished files never reach the shell.
  assert.equal(calls.open, 1);
  assert.equal(calls.reveal, 1);
});

test('a download onto an existing file renames like Chrome instead of overwriting', () => {
  const taken = new Set(['C:\\Downloads\\setup.exe']);
  const session = new EventEmitter();
  const service = createBrowserDownloadService({
    openPath: async () => '',
    getDownloadsPath: () => 'C:\\Downloads',
    pathExists: filePath => taken.has(filePath),
    notifyCoalesceMs: 1,
  });
  service.attach(session, () => {});

  const item = createDownloadItem({ savePath: 'C:\\Downloads\\setup.exe' });
  session.emit('will-download', {}, item);
  // The requested path exists on disk; the item is redirected to a free name.
  assert.equal(item.savePath, 'C:\\Downloads\\setup (1).exe');
  assert.equal(service.list()[0].path, 'C:\\Downloads\\setup (1).exe');

  // A fresh path is left untouched.
  const other = createDownloadItem({ filename: 'new.zip', savePath: 'C:\\Downloads\\new.zip' });
  session.emit('will-download', {}, other);
  assert.equal(other.savePath, 'C:\\Downloads\\new.zip');
});

test('remove drops a record from the list but refuses live downloads', () => {
  const saved = [];
  const session = new EventEmitter();
  const service = createBrowserDownloadService({
    openPath: async () => '',
    getDownloadsPath: () => 'C:\\Downloads',
    storage: { load: () => [], save: records => saved.push(records) },
    notifyCoalesceMs: 1,
  });
  service.attach(session, () => {});

  const active = createDownloadItem({ filename: 'live.exe' });
  session.emit('will-download', {}, active);
  const done = createDownloadItem({ filename: 'done.exe' });
  session.emit('will-download', {}, done);
  done.state = 'completed';
  done.emit('done', {}, 'completed');

  const [doneRecord, activeRecord] = service.list();
  assert.equal(service.remove(doneRecord.id), true);
  assert.deepEqual(service.list().map(record => record.id), [activeRecord.id]);
  // A record with a live DownloadItem must be cancelled instead of removed.
  assert.equal(service.remove(activeRecord.id), false);
  assert.equal(service.remove('missing-id'), false);
  assert.equal(service.list().length, 1);
});

test('a re-download onto the same path supersedes the older record', async () => {
  // Old file was deleted before the re-download, so the conflict rename does
  // not fire and both records end up pointing at the same path — the file on
  // disk now belongs to the newer record (Chrome: IsMostRecentDownloadItemAtPath).
  const disk = new Set();
  const session = new EventEmitter();
  const service = createBrowserDownloadService({
    openPath: async () => '',
    getDownloadsPath: () => 'C:\\Downloads',
    showInFolder: () => {},
    pathExists: filePath => disk.has(filePath),
    notifyCoalesceMs: 1,
  });
  service.attach(session, () => {});

  const old = createDownloadItem({ filename: 'blob', savePath: 'C:\\Downloads\\blob' });
  session.emit('will-download', {}, old);
  old.state = 'completed';
  old.emit('done', {}, 'completed');
  const fresh = createDownloadItem({ filename: 'blob', savePath: 'C:\\Downloads\\blob' });
  session.emit('will-download', {}, fresh);
  fresh.state = 'completed';
  fresh.emit('done', {}, 'completed');
  disk.add('C:\\Downloads\\blob');

  const [freshRecord, oldRecord] = service.list();
  assert.equal(freshRecord.fileMissing, false);
  assert.equal(oldRecord.fileMissing, true);
  // Only the most recent download at a path may act on the file.
  assert.equal(await service.openDownload(freshRecord.id), true);
  assert.equal(await service.openDownload(oldRecord.id), false);
  assert.equal(service.revealInFolder(freshRecord.id), true);
  assert.equal(service.revealInFolder(oldRecord.id), false);
});

test('file icons resolve per extension, backfill in-flight records and are cached', async () => {
  const session = new EventEmitter();
  const iconCalls = [];
  const service = createBrowserDownloadService({
    openPath: async () => '',
    getDownloadsPath: () => 'C:\\Downloads',
    getFileIcon: async filePath => {
      iconCalls.push(filePath);
      return 'data:image/png;base64,ICON';
    },
    notifyCoalesceMs: 1,
  });
  service.attach(session, () => {});

  // Two downloads of the same type start while the first lookup is in flight.
  session.emit('will-download', {}, createDownloadItem({ filename: 'report.pdf', url: 'https://example.com/report.pdf', savePath: 'C:\\Downloads\\report.pdf' }));
  session.emit('will-download', {}, createDownloadItem({ filename: 'other.pdf', url: 'https://example.com/other.pdf', savePath: 'C:\\Downloads\\other.pdf' }));
  session.emit('will-download', {}, createDownloadItem({ filename: 'notes.txt', url: 'https://example.com/notes.txt', savePath: 'C:\\Downloads\\notes.txt' }));
  await new Promise(resolve => setTimeout(resolve, 10));

  const list = service.list();
  const byName = Object.fromEntries(list.map(record => [record.filename, record]));
  assert.equal(byName['report.pdf'].icon, 'data:image/png;base64,ICON');
  assert.equal(byName['other.pdf'].icon, 'data:image/png;base64,ICON');
  assert.equal(byName['notes.txt'].icon, 'data:image/png;base64,ICON');
  // The extension cache collapses same-type lookups into a single OS call.
  assert.equal(iconCalls.length, 2);
  assert.ok(iconCalls.includes('C:\\Downloads\\report.pdf'));

  // A later download of the same type reuses the cache synchronously.
  session.emit('will-download', {}, createDownloadItem({ filename: 'third.pdf', url: 'https://example.com/third.pdf', savePath: 'C:\\Downloads\\third.pdf' }));
  assert.equal(service.list().find(record => record.filename === 'third.pdf').icon, 'data:image/png;base64,ICON');
  assert.equal(iconCalls.length, 2);
});

test('file icon failures leave the record without an icon', async () => {
  const session = new EventEmitter();
  const service = createBrowserDownloadService({
    openPath: async () => '',
    getDownloadsPath: () => 'C:\\Downloads',
    getFileIcon: async () => {
      throw new Error('no icon');
    },
    notifyCoalesceMs: 1,
  });
  service.attach(session, () => {});

  session.emit('will-download', {}, createDownloadItem());
  await new Promise(resolve => setTimeout(resolve, 10));

  assert.equal(service.list()[0].icon, undefined);
});

test('openDownload only opens completed downloads', async () => {
  const opened = [];
  const session = new EventEmitter();
  const service = createBrowserDownloadService({
    openPath: async target => {
      opened.push(target);
      return '';
    },
    getDownloadsPath: () => 'C:\\Downloads',
    notifyCoalesceMs: 1,
  });
  service.attach(session, () => {});

  const item = createDownloadItem();
  session.emit('will-download', {}, item);
  const [record] = service.list();

  assert.equal(await service.openDownload(record.id), false); // still downloading
  item.state = 'completed';
  item.emit('done', {}, 'completed');
  assert.equal(await service.openDownload(record.id), true);
  assert.deepEqual(opened, [item.savePath]);
});

test('restored records without a usable startTime are dropped as malformed', () => {
  const storage = {
    load: () => [
      // startTime 0 would render as "20000+ days ago"; a non-number is equally
      // unusable — both degrade to fewer records like the rest of the checks.
      { id: 'download-1', filename: 'a.exe', state: 'completed', startTime: 0, endTime: 2 },
      { id: 'download-2', filename: 'b.exe', state: 'completed', startTime: '5', endTime: 2 },
      { id: 'download-3', filename: 'c.exe', state: 'completed', startTime: 5, endTime: 6 },
    ],
    save: () => {},
  };
  const service = createBrowserDownloadService({
    openPath: async () => '',
    getDownloadsPath: () => 'C:\\Downloads',
    storage,
    notifyCoalesceMs: 1,
  });
  assert.deepEqual(service.list().map(record => record.id), ['download-3']);
});

test('list caches presence across notify ticks; fresh listings re-check', () => {
  // The disk starts empty and files land on completion — otherwise the
  // will-download conflict rename would redirect the save path.
  const disk = new Set();
  let statCalls = 0;
  const session = new EventEmitter();
  const service = createBrowserDownloadService({
    openPath: async () => '',
    getDownloadsPath: () => 'C:\\Downloads',
    pathExists: filePath => {
      statCalls += 1;
      return disk.has(filePath);
    },
    notifyCoalesceMs: 1,
  });
  service.attach(session, () => {});
  for (const name of ['kept.exe', 'moved.exe']) {
    const item = createDownloadItem({ filename: name, savePath: `C:\\Downloads\\${name}` });
    session.emit('will-download', {}, item);
    item.state = 'completed';
    item.emit('done', {}, 'completed');
    disk.add(item.savePath);
  }
  statCalls = 0;

  // Coalesced notify ticks hit each unique path once, then serve from cache.
  service.list();
  assert.equal(statCalls, 2);
  service.list();
  service.list();
  assert.equal(statCalls, 2);

  // Deleting the file externally does not re-stat on cached ticks: the cached
  // listing keeps its last-known state (and costs zero stats).
  disk.delete('C:\\Downloads\\kept.exe');
  const cached = service.list();
  assert.equal(cached.find(record => record.filename === 'kept.exe').fileMissing, false);
  assert.equal(statCalls, 2);

  // IPC listings re-check, so a file deleted outside the app shows up there —
  // and the cache picks up the new state for subsequent ticks.
  const fresh = service.list({ freshPresence: true });
  assert.equal(statCalls, 4);
  assert.equal(fresh.find(record => record.filename === 'kept.exe').fileMissing, true);
  assert.equal(service.list().find(record => record.filename === 'kept.exe').fileMissing, true);
  assert.equal(statCalls, 4);
});

test('records are capped to the newest MAX_DOWNLOAD_RECORDS entries', async () => {
  const session = new EventEmitter();
  const service = createBrowserDownloadService({
    openPath: async () => '',
    getDownloadsPath: () => 'C:\\Downloads',
    notifyCoalesceMs: 1,
  });
  service.attach(session, () => {});

  for (let index = 0; index < MAX_DOWNLOAD_RECORDS + 5; index += 1) {
    session.emit('will-download', {}, createDownloadItem({ filename: `file-${index}.bin` }));
  }
  const list = service.list();
  assert.equal(list.length, MAX_DOWNLOAD_RECORDS);
  assert.equal(list[0].filename, `file-${MAX_DOWNLOAD_RECORDS + 4}.bin`);
});

test('openFolder opens the downloads directory', async () => {
  const opened = [];
  const session = new EventEmitter();
  const service = createBrowserDownloadService({
    openPath: async target => {
      opened.push(target);
      return '';
    },
    getDownloadsPath: () => 'C:\\Downloads',
    notifyCoalesceMs: 1,
  });
  assert.equal(await service.openFolder(), true);
  assert.deepEqual(opened, ['C:\\Downloads']);
  openPathsOf(opened);
});
