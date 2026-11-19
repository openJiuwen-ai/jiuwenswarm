import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { webClient } from '../../services/webClient';
import { Dialog } from '../../components/ui/Dialog/Dialog';
import { useSessionStore } from '../../stores/sessionStore';
import { useWorkspaceStore } from '../../stores/workspaceStore';
import { groupChatClient, type GroupInfo } from './client';
import { MemberPicker } from './MemberPicker';

/** Group list plus the create, rename, pin and delete flows shared by the chat sidebar and the open group. */
export interface GroupDirectory {
  groups: GroupInfo[];
  loading: boolean;
  error: string;
  hasMore: boolean;
  loadingMore: boolean;
  pinnedIds: ReadonlySet<string>;
  pinningId: string | null;
  /** Latest saved copy of a group, so an open conversation can pick up renames without refetching. */
  lastUpdated: GroupInfo | null;
  reload: () => Promise<void>;
  loadMore: () => void;
  openCreate: () => void;
  openEdit: (group: GroupInfo) => void;
  openDelete: (group: GroupInfo) => void;
  pin: (group: GroupInfo, pinned: boolean) => void;
  /** Dialogs owned by the directory; render once wherever the directory is used. */
  dialogs: ReactNode;
}

export function useGroupDirectory({ activeGroupId, onSelect }: {
  activeGroupId?: string;
  onSelect: (groupId?: string) => void;
}): GroupDirectory {
  const { t } = useTranslation();
  const [groups, setGroups] = useState<GroupInfo[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [pinningId, setPinningId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editTarget, setEditTarget] = useState<GroupInfo | null>(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [saving, setSaving] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<GroupInfo | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<GroupInfo | null>(null);
  const pinnedSessions = useWorkspaceStore((state) => state.pinnedSessions);
  const alive = useRef(true);
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;

  const report = useCallback((reason: unknown) => {
    if (alive.current) setError(reason instanceof Error ? reason.message : String(reason));
  }, []);

  const reload = useCallback(async () => {
    try {
      const page = await groupChatClient.list({ limit: 50 });
      if (!alive.current) return;
      setGroups(page.groups);
      setTotal(page.total);
      setError('');
    } catch (reason) { report(reason); }
    finally { if (alive.current) setLoading(false); }
  }, [report]);

  useEffect(() => {
    alive.current = true;
    void reload();
    void useWorkspaceStore.getState().loadPinnedSessions();
    const stop = webClient.onStateChange((state) => {
      if (state !== 'ready') return;
      void reload();
      void useWorkspaceStore.getState().loadPinnedSessions();
    });
    return () => { alive.current = false; stop(); };
  }, [reload]);

  // Keep the list fresh when the open group changes (new group, member added elsewhere).
  useEffect(() => {
    if (activeGroupId && !groups.some((item) => item.group_id === activeGroupId)) void reload();
  }, [activeGroupId, groups, reload]);

  const loadMore = useCallback(() => {
    if (loadingMore) return;
    setLoadingMore(true);
    groupChatClient.list({ limit: 50, offset: groups.length })
      .then((page) => {
        if (!alive.current) return;
        setGroups((current) => [...current, ...page.groups]);
        setTotal(page.total);
      })
      .catch(report)
      .finally(() => { if (alive.current) setLoadingMore(false); });
  }, [groups.length, loadingMore, report]);

  const pin = useCallback((item: GroupInfo, pinned: boolean) => {
    if (pinningId !== null) return;
    setPinningId(item.group_id);
    useWorkspaceStore.getState().pinSession(item.session_id, pinned)
      .catch(report)
      .finally(() => { if (alive.current) setPinningId(null); });
  }, [pinningId, report]);

  const openEdit = useCallback((target: GroupInfo) => {
    setError(''); setName(target.name); setDescription(target.description); setEditTarget(target);
  }, []);

  const openDelete = useCallback((target: GroupInfo) => {
    setError(''); setDeleteTarget(target);
  }, []);

  async function update() {
    if (!editTarget || !name.trim() || saving) return;
    setSaving(true);
    try {
      const updated = await groupChatClient.update(editTarget.group_id, { name: name.trim(), description });
      if (!alive.current) return;
      setGroups((current) => current.map((item) => item.group_id === updated.group_id ? updated : item));
      setLastUpdated(updated);
      setEditTarget(null);
      void useWorkspaceStore.getState().refreshWorkspaceData();
    } catch (reason) { report(reason); }
    finally { if (alive.current) setSaving(false); }
  }

  async function remove() {
    if (!deleteTarget || deleting) return;
    const targetId = deleteTarget.group_id;
    setDeleting(true);
    try {
      await groupChatClient.delete(targetId);
      useSessionStore.getState().removeSession(targetId);
      void useWorkspaceStore.getState().refreshWorkspaceData();
      if (!alive.current) return;
      setDeleteTarget(null);
      setGroups((current) => current.filter((item) => item.group_id !== targetId));
      setTotal((current) => Math.max(0, current - 1));
      if (targetId === activeGroupId) selectRef.current();
    } catch (reason) { report(reason); }
    finally { if (alive.current) setDeleting(false); }
  }

  const dialogs = <>
    {creating && <MemberPicker onClose={() => setCreating(false)} onSaved={(saved) => {
      setCreating(false);
      setGroups((current) => [saved, ...current.filter((item) => item.group_id !== saved.group_id)]);
      setTotal((current) => current + 1);
      void useWorkspaceStore.getState().refreshWorkspaceData();
      selectRef.current(saved.group_id);
    }} />}
    <Dialog open={Boolean(editTarget)} titleId="group-chat-edit-title" className="group-chat-small-dialog" closeDisabled={saving}
      onCancel={() => setEditTarget(null)}>
      {error && <p role="alert" className="group-chat-error" data-testid="group-chat-edit-error">{error}</p>}
      <form data-testid="group-chat-edit-form" onSubmit={(event) => { event.preventDefault(); void update(); }}>
        <h2 id="group-chat-edit-title">{t('groupChat.edit')}</h2>
        <label>{t('groupChat.name')}<input value={name} maxLength={200} disabled={saving}
          onChange={(event) => setName(event.target.value)} data-testid="group-chat-edit-name" /></label>
        <label>{t('groupChat.description')}<textarea value={description} maxLength={2000} disabled={saving}
          onChange={(event) => setDescription(event.target.value)} data-testid="group-chat-edit-description" /></label>
        <footer className="group-chat-dialog-footer"><button type="button" className="btn" disabled={saving}
          onClick={() => setEditTarget(null)} data-testid="group-chat-edit-cancel">{t('common.cancel')}</button>
          <button type="submit" className="btn primary" disabled={saving || !name.trim()}
            data-testid="group-chat-edit-save">{t('common.save')}</button></footer>
      </form>
    </Dialog>
    <Dialog open={Boolean(deleteTarget)} titleId="group-chat-delete-title" className="group-chat-small-dialog"
      closeDisabled={deleting} onCancel={() => setDeleteTarget(null)}>
      <div data-testid="group-chat-delete-dialog"><h2 id="group-chat-delete-title">{t('groupChat.delete')}</h2>
        {error && <p role="alert" className="group-chat-error" data-testid="group-chat-delete-error">{error}</p>}
        {deleteTarget && <p className="group-chat-delete-name" data-testid="group-chat-delete-name">{deleteTarget.name}</p>}
        <p data-testid="group-chat-delete-warning">{t('groupChat.deleteWarning')}</p>
        <footer className="group-chat-dialog-footer"><button type="button" className="btn" disabled={deleting}
          onClick={() => setDeleteTarget(null)} data-testid="group-chat-delete-cancel">{t('common.cancel')}</button>
          <button type="button" className="btn group-chat-delete" disabled={deleting} onClick={() => void remove()}
            data-testid="group-chat-delete-confirm">{t('groupChat.delete')}</button></footer>
      </div>
    </Dialog>
  </>;

  return {
    groups, loading, error, hasMore: groups.length < total, loadingMore,
    pinnedIds: new Set(pinnedSessions.map((item) => item.session_id)), pinningId, lastUpdated,
    reload, loadMore, openCreate: () => setCreating(true), openEdit, openDelete, pin, dialogs,
  };
}
