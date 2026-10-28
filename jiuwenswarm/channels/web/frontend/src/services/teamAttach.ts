// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { webClient } from './webClient';

/**
 * Attach the current Web connection to a Team Session event stream.
 *
 * `chat.send + attach_only` registers a persistent waiter without submitting
 * input or creating a Team runtime. The stream intentionally has no response
 * deadline and lives until the WebSocket connection closes.
 */

const attachedSessions = new Set<string>();

export function isTeamSessionAttached(sessionId: string): boolean {
  return attachedSessions.has(sessionId);
}

/** Clear connection-scoped attachment state after a WebSocket reconnect. */
export function resetTeamAttachments(): void {
  attachedSessions.clear();
}

/**
 * Attach once per Session for the current WebSocket connection.
 *
 * A synchronous send/connect failure clears the marker so a later navigation
 * can retry. Successful delivery remains attached until reconnect reset.
 */
export async function attachTeamSession(
  sessionId: string,
  mode?: string
): Promise<void> {
  if (!sessionId || sessionId === 'new' || attachedSessions.has(sessionId)) {
    return;
  }
  attachedSessions.add(sessionId);
  try {
    await webClient.sendFireAndForget(
      'chat.send',
      {
        session_id: sessionId,
        query: '',
        ...(mode ? { mode } : {}),
        attach_only: true,
      },
      { isStream: true }
    );
  } catch (error) {
    attachedSessions.delete(sessionId);
    throw error;
  }
}
