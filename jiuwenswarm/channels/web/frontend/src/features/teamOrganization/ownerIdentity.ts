// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useSessionStore } from '../../stores/sessionStore';

export function ownerTeamId(sessionId: string): string | undefined {
  const state = useSessionStore.getState();
  return (
    state.currentSession?.session_id === sessionId
      ? state.currentSession
      : state.sessions.find((session) => session.session_id === sessionId)
  )?.team_name?.trim();
}
