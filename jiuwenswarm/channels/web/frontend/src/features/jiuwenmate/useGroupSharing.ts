import { useEffect, useRef, useState } from 'react';
import {
  canDecideSharing,
  type GroupSharingApi,
  type GroupSharingSnapshot,
  type SharingDecision,
} from './groupSharingApi';

interface InboxState {
  api: GroupSharingApi;
  configId: string;
  data: GroupSharingSnapshot | null;
  loading: boolean;
  loadError: boolean;
  actionError: boolean;
  busy: string | null;
}

/** Query independently of ordinary Requests. No card content is sent to chat or the model. */
export function useGroupSharing(api: GroupSharingApi, configId: string, connectionEpoch: number) {
  const [state, setState] = useState<InboxState | null>(null);
  const [now, setNow] = useState(Date.now);
  const operations = useRef<{
    refresh(): void;
    decide(id: string, decision: SharingDecision): void;
    revoke(id: string): void;
  } | null>(null);

  useEffect(() => {
    let active = true;
    let sequence = 0;
    let querying = false;
    let busy = false;
    let current: InboxState = { api, configId, data: null, loading: true, loadError: false, actionError: false, busy: null };
    const update = (patch: Partial<InboxState>) => {
      if (!active) return;
      current = { ...current, ...patch };
      setState(current);
    };
    const refresh = async (foreground = false) => {
      setNow(Date.now());
      if (!active || querying || busy) return;
      querying = true;
      const token = ++sequence;
      // Background polls keep the existing card, focus and controls steady.
      if (foreground || current.data === null) update({ loading: true });
      try {
        const data = await api.list(configId);
        if (active && token === sequence) update({ data, loadError: false });
      } catch {
        if (active && token === sequence) update({ loadError: true });
      } finally {
        if (active && token === sequence) {
          querying = false;
          update({ loading: false });
        }
      }
    };
    const mutate = async (key: string, action: () => Promise<GroupSharingSnapshot>) => {
      if (!active || busy || current.loading || current.loadError) return;
      busy = true;
      // A list response started before a decision must never restore stale pending actions.
      ++sequence;
      querying = false;
      update({ busy: key, actionError: false });
      let failed = false;
      try {
        const data = await action();
        if (active) update({ data });
      } catch {
        failed = true;
        update({ actionError: true, loadError: true });
      } finally {
        busy = false;
        update({ busy: null });
      }
      // A lost response may have committed on the server. Reconcile before allowing another action.
      if (failed && active) void refresh();
    };
    operations.current = {
      refresh: () => { void refresh(true); },
      decide: (id, decision) => {
        const approval = current.data?.approvals.find((item) => item.approval_id === id);
        if (approval && canDecideSharing(approval, Date.now())) {
          void mutate(`approval:${id}`, () => api.decide(configId, id, decision));
        }
      },
      revoke: (id) => {
        if (current.data?.grants.some((item) => item.grant_id === id && item.status === 'active')) {
          void mutate(`grant:${id}`, () => api.revoke(configId, id));
        }
      },
    };
    update({});
    void refresh();
    const poll = () => { void refresh(); };
    const visible = () => { if (document.visibilityState === 'visible') poll(); };
    const timer = window.setInterval(poll, 5000);
    window.addEventListener('focus', poll);
    document.addEventListener('visibilitychange', visible);
    return () => {
      active = false;
      operations.current = null;
      window.clearInterval(timer);
      window.removeEventListener('focus', poll);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [api, configId, connectionEpoch]);

  const current = state?.api === api && state.configId === configId ? state : null;
  return {
    data: current?.data ?? null,
    loading: current?.loading ?? true,
    loadError: current?.loadError ?? false,
    actionError: current?.actionError ?? false,
    busy: current?.busy ?? null,
    now,
    refresh: () => operations.current?.refresh(),
    decide: (id: string, decision: SharingDecision) => operations.current?.decide(id, decision),
    revoke: (id: string) => operations.current?.revoke(id),
  };
}
