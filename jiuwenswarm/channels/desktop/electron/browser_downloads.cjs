// Download history for the shared browser partition — the data source behind
// the Chrome-style download bubble in the built-in browser pane. The bubble
// semantics are "recent downloads", so records live in memory only and are
// capped; a durable download history would need a storage story of its own.
const MAX_DOWNLOAD_RECORDS = 100;
// DownloadItem 'updated' fires per chunk. Coalesce notifications so the
// renderer re-renders the list at most once per tick instead of per chunk.
const NOTIFY_COALESCE_MS = 120;
const KNOWN_DOWNLOAD_STATES = new Set(['downloading', 'paused', 'completed', 'cancelled', 'interrupted']);

// Validate a record restored from disk; returns null for malformed entries so
// a corrupt or hand-edited store degrades to fewer records instead of failing.
function sanitizeRestoredRecord(raw) {
  if (!raw || typeof raw !== 'object') return null;
  if (!KNOWN_DOWNLOAD_STATES.has(raw.state)) return null;
  if (typeof raw.id !== 'string' || !raw.id) return null;
  // Without a usable startTime the record has neither ordering nor a sensible
  // "x ago" display (a 0 timestamp would render as thousands of days ago).
  if (!(typeof raw.startTime === 'number' && Number.isFinite(raw.startTime) && raw.startTime > 0)) return null;
  const nonNegative = value => (typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : 0);
  return {
    id: raw.id,
    filename: typeof raw.filename === 'string' && raw.filename ? raw.filename : 'download',
    url: typeof raw.url === 'string' ? raw.url : '',
    path: typeof raw.path === 'string' ? raw.path : '',
    receivedBytes: nonNegative(raw.receivedBytes),
    totalBytes: nonNegative(raw.totalBytes),
    state: raw.state,
    startTime: raw.startTime,
    endTime: nonNegative(raw.endTime),
  };
}

// "C:\\downloads\\a.zip" -> "C:\\downloads\\a (1).zip", first free index, like
// Chrome's auto-rename. Pure string handling (no node:path) so behavior and
// tests stay platform-independent. Returns null when no free name was found.
function renameConflictingPath(savePath, pathExists) {
  const slashIndex = Math.max(savePath.lastIndexOf('/'), savePath.lastIndexOf('\\'));
  const dotIndex = savePath.lastIndexOf('.');
  const hasExtension = dotIndex > slashIndex;
  const directory = slashIndex >= 0 ? savePath.slice(0, slashIndex + 1) : '';
  const base = hasExtension ? savePath.slice(slashIndex + 1, dotIndex) : savePath.slice(slashIndex + 1);
  const extension = hasExtension ? savePath.slice(dotIndex) : '';
  for (let index = 1; index <= 100; index += 1) {
    const candidate = `${directory}${base} (${index})${extension}`;
    if (!pathExists(candidate)) return candidate;
  }
  return null;
}

function createBrowserDownloadService({ openPath, getDownloadsPath, getFileIcon = null, showInFolder = null, pathExists = null, storage = null, notifyCoalesceMs = NOTIFY_COALESCE_MS } = {}) {
  if (typeof openPath !== 'function') throw new Error('openPath is required');
  if (typeof getDownloadsPath !== 'function') throw new Error('getDownloadsPath is required');
  const records = []; // newest first
  const itemsById = new Map(); // record id -> DownloadItem, for pause/resume/cancel
  // System file icons are per-extension; cache them across records. The
  // pending map also dedupes concurrent lookups for the same extension.
  const iconCache = new Map(); // extension (lowercase) -> data URL
  const iconPending = new Map(); // extension -> in-flight lookup promise
  // Presence cache for the "deleted" marker. pathExists is a sync stat and
  // list() runs on every coalesced notify tick for up to MAX_DOWNLOAD_RECORDS
  // completed records — on OneDrive folders, network drives or behind Defender
  // real-time scanning each stat can take milliseconds, which would stall the
  // main process every tick. Cache by path; lifecycle events (done/remove) and
  // fresh-presence listings (IPC, i.e. when the bubble actually opens — the
  // same moment Chrome re-checks) keep it coherent.
  const presenceCache = new Map(); // path -> boolean
  const cachedPathExists = filePath => {
    let exists = presenceCache.get(filePath);
    if (exists === undefined) {
      exists = pathExists(filePath);
      presenceCache.set(filePath, exists);
    }
    return exists;
  };
  let sequence = 0;
  let notifyTimer = null;
  let notifyCallback = null;

  // Restore the durable history (see main.cjs browser-downloads.json). Live
  // DownloadItems cannot survive a restart, so unfinished records are marked
  // interrupted — same as Chrome marking crashed downloads interrupted.
  if (storage && typeof storage.load === 'function') {
    let stored = [];
    try {
      stored = storage.load();
    } catch {
      stored = [];
    }
    if (!Array.isArray(stored)) stored = [];
    for (const raw of stored) {
      const record = sanitizeRestoredRecord(raw);
      if (record) records.push(record);
    }
    for (const record of records) {
      if (record.state === 'downloading' || record.state === 'paused') record.state = 'interrupted';
      const match = /^download-(\d+)$/.exec(record.id);
      if (match) sequence = Math.max(sequence, Number(match[1]));
    }
  }

  // Chrome's CanOpenDownload/IsMostRecentDownloadItemAtFilePath semantics, in
  // two parts: (1) the file at the recorded path must still exist; (2) the file
  // must belong to THIS record — records are newest-first, so an older record
  // sharing its path with a newer completed download is superseded: the file
  // on disk was written by that newer download. This keeps a re-download of a
  // deleted filename from "reviving" the older, already-deleted record.
  const isFileMissing = (record, index = records.indexOf(record)) => {
    if (typeof pathExists === 'function' && !cachedPathExists(record.path)) return true;
    for (let before = 0; before < index; before += 1) {
      const newer = records[before];
      if (newer.state === 'completed' && newer.path === record.path) return true;
    }
    return false;
  };

  // freshPresence: bypass the presence cache so IPC-driven listings (bubble
  // open, panel mount) re-check the filesystem, like Chrome re-checking when
  // its UI opens. The coalesced-notify path uses the default cached listing.
  const list = ({ freshPresence = false } = {}) => records.map((record, index) => {
    const copy = { ...record };
    // Chrome-style "deleted" marker — runtime-only, never persisted.
    if (copy.state === 'completed' && copy.path) {
      if (freshPresence) presenceCache.delete(record.path);
      copy.fileMissing = isFileMissing(record, index);
    }
    return copy;
  });

  const scheduleNotify = () => {
    if (notifyTimer) return;
    notifyTimer = setTimeout(() => {
      notifyTimer = null;
      if (notifyCallback) notifyCallback(list());
      if (storage && typeof storage.save === 'function') {
        // Icons are runtime-only (multi-KB data URLs); never persist them.
        storage.save(records.map(record => {
          const { icon: _icon, ...rest } = record;
          return rest;
        }));
      }
    }, notifyCoalesceMs);
  };

  // DownloadItem methods may throw once its webContents is gone; reads are
  // best-effort everywhere.
  const readItem = (item, reader) => {
    try {
      const value = reader();
      return value === undefined ? null : value;
    } catch {
      return null;
    }
  };

  const syncRecordProgress = (record, item) => {
    record.receivedBytes = Math.max(0, readItem(item, () => item.getReceivedBytes()) ?? record.receivedBytes);
    record.totalBytes = Math.max(0, readItem(item, () => item.getTotalBytes()) ?? record.totalBytes);
    record.path = readItem(item, () => item.getSavePath()) || record.path;
  };

  // Attach the OS file-type icon (the same one Explorer/Chrome show) to the
  // record as a data URL. Best-effort: any failure leaves `icon` unset and the
  // renderer falls back to its generic SVG. Icons are per-extension, so the
  // cache covers every record; the in-flight lookup backfills all of them on
  // resolve, so records created mid-lookup need no extra wiring.
  const enrichRecordIcon = record => {
    if (typeof getFileIcon !== 'function' || record.icon) return;
    const filename = String(record.filename || '');
    const dotIndex = filename.lastIndexOf('.');
    const extension = dotIndex >= 0 ? filename.slice(dotIndex).toLowerCase() : '';
    if (!extension) return;
    const cached = iconCache.get(extension);
    if (cached) {
      record.icon = cached;
      return;
    }
    if (iconPending.has(extension)) return;
    const lookup = (async () => {
      // The icon depends on the file extension only; prefer the real save
      // path, fall back to a synthesized one inside the downloads folder.
      const target = record.path || `${getDownloadsPath()}/${filename}`;
      const dataUrl = await getFileIcon(target);
      if (dataUrl) iconCache.set(extension, dataUrl);
      return dataUrl;
    })().catch(() => null);
    iconPending.set(extension, lookup);
    void lookup.then(dataUrl => {
      iconPending.delete(extension);
      if (!dataUrl) return;
      for (const entry of records) {
        const entryName = String(entry.filename || '');
        const entryExt = entryName.slice(entryName.lastIndexOf('.')).toLowerCase();
        if (!entry.icon && entryExt === extension) entry.icon = dataUrl;
      }
      scheduleNotify();
    });
  };

  const attach = (partitionSession, onNotify) => {
    if (!partitionSession) throw new Error('partition session is required');
    // session.fromPartition returns the same instance per partition; guard
    // against double-attaching when ensureBrowserView runs repeatedly.
    if (partitionSession.__jiuwenDownloadTrackingAttached) return;
    partitionSession.__jiuwenDownloadTrackingAttached = true;
    notifyCallback = typeof onNotify === 'function' ? onNotify : null;
    // Restored records carry no icon (icons are runtime-only, not persisted);
    // backfill system icons now that the pane is actually in use.
    for (const record of records) enrichRecordIcon(record);
    partitionSession.on('will-download', (_event, item) => {
      // Chrome-like conflict handling: never overwrite a file already on disk;
      // save as "name (1).ext" instead. Each record keeps its own file, so a
      // re-download cannot "revive" an older record marked 已删除.
      const requestedPath = readItem(item, () => item.getSavePath()) || '';
      if (requestedPath && typeof pathExists === 'function' && pathExists(requestedPath)) {
        const renamed = renameConflictingPath(requestedPath, pathExists);
        if (renamed) readItem(item, () => item.setSavePath(renamed));
      }
      sequence += 1;
      const record = {
        id: `download-${sequence}`,
        filename: readItem(item, () => item.getFilename()) || 'download',
        url: readItem(item, () => item.getURL()) || '',
        path: readItem(item, () => item.getSavePath()) || '',
        receivedBytes: 0,
        totalBytes: 0,
        // 'downloading' | 'paused' | 'completed' | 'cancelled' | 'interrupted'
        state: 'downloading',
        startTime: Date.now(),
        endTime: 0,
      };
      records.unshift(record);
      if (records.length > MAX_DOWNLOAD_RECORDS) {
        const dropped = records.pop();
        itemsById.delete(dropped.id);
      }
      itemsById.set(record.id, item);
      enrichRecordIcon(record);
      item.on('updated', () => {
        syncRecordProgress(record, item);
        record.state = item.isPaused() ? 'paused' : 'downloading';
        scheduleNotify();
      });
      item.once('done', () => {
        syncRecordProgress(record, item);
        record.state = readItem(item, () => item.getState()) || 'interrupted';
        record.endTime = Date.now();
        // The file just landed on (or vanished from) disk; re-check next list.
        presenceCache.delete(record.path);
        // Finished downloads accept no further pause/resume/cancel actions.
        itemsById.delete(record.id);
        scheduleNotify();
      });
      scheduleNotify();
    });
  };

  const action = (downloadId, requestedAction) => {
    const item = itemsById.get(String(downloadId));
    if (!item) return false;
    try {
      if (requestedAction === 'pause') {
        if (!item.isPaused()) item.pause();
        return true;
      }
      if (requestedAction === 'resume') {
        if (item.isPaused() && item.canResume()) item.resume();
        return true;
      }
      if (requestedAction === 'cancel') {
        item.cancel();
        return true;
      }
    } catch {
      return false;
    }
    return false;
  };

  const openDownload = async downloadId => {
    const record = records.find(entry => entry.id === String(downloadId));
    if (!record?.path || record.state !== 'completed') return false;
    // User-initiated action: bypass the presence cache for the current state.
    presenceCache.delete(record.path);
    if (isFileMissing(record)) return false;
    const message = await openPath(record.path);
    return !message;
  };

  // Reveal a completed download in the system file manager (Explorer/Finder).
  const revealInFolder = downloadId => {
    const record = records.find(entry => entry.id === String(downloadId));
    if (!record?.path || record.state !== 'completed') return false;
    // User-initiated action: bypass the presence cache for the current state.
    presenceCache.delete(record.path);
    if (isFileMissing(record)) return false;
    if (typeof showInFolder !== 'function') return false;
    showInFolder(record.path);
    return true;
  };

  const openFolder = async () => {
    const message = await openPath(getDownloadsPath());
    return !message;
  };

  // Chrome's "remove from list": drop a record from the history. Records with
  // a live DownloadItem are refused — cancel those instead of orphaning them.
  const remove = downloadId => {
    const id = String(downloadId);
    const index = records.findIndex(entry => entry.id === id);
    if (index < 0 || itemsById.has(id)) return false;
    presenceCache.delete(records[index].path);
    records.splice(index, 1);
    scheduleNotify();
    return true;
  };

  return { attach, list, action, openDownload, revealInFolder, openFolder, remove };
}

module.exports = { createBrowserDownloadService, MAX_DOWNLOAD_RECORDS };
