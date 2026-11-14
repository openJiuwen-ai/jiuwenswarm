/**
 * Notes Vault - workspace page.
 *
 * Contributed as a `bundled` application plugin page; the core frontend
 * resolves the default export by `plugin_id` through its Vite
 * `import.meta.glob` over the `extensions/<name>/frontend/index.tsx` pattern.
 *
 * All data access goes through the `notes.*` RPC namespace, scoped to the
 * caller's session by the backend, so notes never cross sessions.
 */
import { useCallback, useEffect, useState } from 'react';

import { webRequest } from '../../../channels/web/frontend/src/services/webClient';

import {
  NotesVaultPanel,
  type Note,
  type NoteDraft,
  type VaultStats,
} from './NotesVaultPanel';
import './notesVault.css';

/** Must match `plugin_id` in extension.py and `id` in extension.yaml. */
export const applicationPluginId = 'notes-vault';

const EMPTY_DRAFT: NoteDraft = { title: '', body: '', tags: '', pinned: false };

const toDraft = (note: Note): NoteDraft => ({
  title: note.title,
  body: note.body,
  tags: note.tags.join(', '),
  pinned: note.pinned,
});

const parseTags = (raw: string): string[] =>
  raw.split(',').map((item) => item.trim()).filter(Boolean);

function NotesVaultPage() {
  const [notes, setNotes] = useState<Note[]>([]);
  const [stats, setStats] = useState<VaultStats | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState<NoteDraft>(EMPTY_DRAFT);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (search: string) => {
    setLoading(true);
    setError(null);
    try {
      const [list, counters] = await Promise.all([
        webRequest<{ notes?: Note[] }>('notes.list', search ? { query: search } : {}),
        webRequest<VaultStats>('notes.stats', {}),
      ]);
      setNotes(list.notes ?? []);
      setStats(counters);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh('');
  }, [refresh]);

  const onSelect = useCallback((note: Note) => {
    setSelectedId(note.id);
    setDraft(toDraft(note));
  }, []);

  const onNew = useCallback(() => {
    setSelectedId(null);
    setDraft({ ...EMPTY_DRAFT });
  }, []);

  const onSave = useCallback(async () => {
    setError(null);
    try {
      const payload = {
        title: draft.title,
        body: draft.body,
        tags: parseTags(draft.tags),
        pinned: draft.pinned,
      };
      if (selectedId) {
        await webRequest('notes.update', { note_id: selectedId, ...payload });
      } else {
        const created = await webRequest<{ note?: Note }>('notes.create', payload);
        if (created.note) {
          setSelectedId(created.note.id);
        }
      }
      await refresh(query);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [draft, selectedId, query, refresh]);

  const onDelete = useCallback(async () => {
    if (!selectedId) {
      return;
    }
    setError(null);
    try {
      await webRequest('notes.delete', { note_id: selectedId });
      onNew();
      await refresh(query);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [selectedId, onNew, query, refresh]);

  const onExport = useCallback(async () => {
    setError(null);
    try {
      const result = await webRequest<{ markdown?: string }>('notes.export', {});
      const url = URL.createObjectURL(
        new Blob([result.markdown ?? ''], { type: 'text/markdown' }),
      );
      const link = document.createElement('a');
      link.href = url;
      link.download = 'notes.md';
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  const onSearch = useCallback(
    (next: string) => {
      setQuery(next);
      void refresh(next);
    },
    [refresh],
  );

  return (
    <NotesVaultPanel
      notes={notes} stats={stats} selectedId={selectedId} draft={draft}
      query={query} loading={loading} error={error}
      onSelect={onSelect} onNew={onNew} onSearch={onSearch} onDraftChange={setDraft}
      onSave={() => void onSave()} onDelete={() => void onDelete()}
      onExport={() => void onExport()}
    />
  );
}

export default NotesVaultPage;
