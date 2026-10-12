// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useCallback, useEffect, useRef, useState } from 'react';
import { useTeamSelectorStore } from '../../stores/teamSelectorStore';
import { webClient } from '../../services/webClient';
import { useTeamOrganizationUiEnabled } from './featureConfig';

const REFRESH_INTERVAL_MS = 10_000;

/** Resolve the exact Team before querying; mounted panels own their refresh lifecycle. */
export function useOrganizationSnapshot<T>(sessionId: string | null) {
  const enabled = useTeamOrganizationUiEnabled();
  const runtime = useTeamSelectorStore((state) => (sessionId ? state.runtimes[sessionId] : undefined));
  const teamId = runtime?.selectedTeamId ?? runtime?.defaultTeamId ?? null;
  const key = `${sessionId ?? ''}:${teamId ?? ''}`;
  const [state, setState] = useState<{
    key: string;
    snapshot: T | null;
    loading: boolean;
    error: string | null;
  }>({ key: '', snapshot: null, loading: false, error: null });
  const sequence = useRef(0);
  const pending = useRef<{ key: string; sequence: number } | null>(null);
  const fetchTeams = useTeamSelectorStore((store) => store.fetchTeams);

  const refresh = useCallback(async () => {
    if (!enabled || !sessionId) return;
    if (!teamId) {
      if (!useTeamSelectorStore.getState().runtimes[sessionId]?.loading) await fetchTeams(sessionId);
      return;
    }
    if (pending.current?.key === key) return;
    const requestSequence = ++sequence.current;
    pending.current = { key, sequence: requestSequence };
    setState((previous) => ({
      key,
      snapshot: previous.key === key ? previous.snapshot : null,
      loading: true,
      error: null,
    }));
    try {
      const snapshot = await webClient.request<T>(
        'org.snapshot',
        { session_id: sessionId, team_id: teamId },
        { timeoutMs: 8000 },
      );
      if (sequence.current !== requestSequence) return;
      setState({ key, snapshot: snapshot ?? null, loading: false, error: null });
    } catch (error) {
      if (sequence.current !== requestSequence) return;
      setState((previous) => ({
        ...previous,
        loading: false,
        error: error instanceof Error ? error.message : String(error),
      }));
    } finally {
      if (pending.current?.sequence === requestSequence) pending.current = null;
    }
  }, [enabled, sessionId, teamId, key, fetchTeams]);

  // Do not depend on the input area's TeamSelector being mounted.
  useEffect(() => {
    if (!enabled || !sessionId) return;
    if (!useTeamSelectorStore.getState().runtimes[sessionId]?.loading) void fetchTeams(sessionId);
  }, [enabled, sessionId, fetchTeams]);

  useEffect(() => {
    void refresh();
    return () => {
      sequence.current += 1;
      pending.current = null;
    };
  }, [refresh]);

  useEffect(() => {
    if (!enabled || !sessionId) return;
    let eventTimer: ReturnType<typeof setTimeout> | undefined;
    const unsubs = ['team.task', 'team.member', 'chat.tool_result', 'chat.final'].map((event) =>
      webClient.on(event, ({ payload }) => {
        if (String(payload.session_id ?? payload.product_session_id ?? '') !== sessionId) return;
        if (eventTimer !== undefined) clearTimeout(eventTimer);
        eventTimer = setTimeout(() => void refresh(), 200);
      }),
    );
    const timer = setInterval(() => {
      if (document.visibilityState !== 'hidden') void refresh();
    }, REFRESH_INTERVAL_MS);
    return () => {
      clearInterval(timer);
      if (eventTimer !== undefined) clearTimeout(eventTimer);
      unsubs.forEach((unsubscribe) => unsubscribe());
    };
  }, [enabled, sessionId, refresh]);

  const current = state.key === key;
  const error = current ? state.error : null;
  return {
    snapshot: enabled && current ? state.snapshot : null,
    loading:
      enabled && Boolean(sessionId) && (teamId ? !current || state.loading : !runtime?.loaded && !runtime?.error),
    error: error ?? (!teamId ? (runtime?.error ?? null) : null),
    noTeam: enabled && Boolean(runtime?.loaded) && !teamId && !runtime?.error,
    refresh,
  };
}
