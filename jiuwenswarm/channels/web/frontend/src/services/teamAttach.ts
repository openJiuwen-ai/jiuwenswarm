// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { webClient } from './webClient';
import { isTeamAgentMode } from '../features/planMode/wireMode';

/**
 * Attach the current Web connection to a Team Session event stream.
 *
 * `chat.send + attach_only` registers a persistent waiter without submitting
 * input or creating a Team runtime. The stream intentionally has no response
 * deadline and lives until the WebSocket connection closes.
 */

const attachedSessions = new Set<string>();
let attachmentGeneration = 0;

export function isTeamSessionAttached(sessionId: string): boolean {
  return attachedSessions.has(sessionId);
}

/** Clear connection-scoped attachment state after a WebSocket reconnect. */
export function resetTeamAttachments(): void {
  attachmentGeneration += 1;
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
  _mode?: string
): Promise<void> {
  if (!sessionId || sessionId === 'new' || attachedSessions.has(sessionId)) {
    return;
  }
  attachedSessions.add(sessionId);
  const generation = attachmentGeneration;
  try {
    // Navigation can restore a cached Team mode before the Session metadata
    // arrives. Confirm its stored type before sending a passive subscription.
    const metadata = await webClient.request<{ mode?: string }>(
      'session.get_metadata',
      { session_id: sessionId },
      { timeoutMs: 10_000 }
    );
    if (generation !== attachmentGeneration) return;
    if (!isTeamAgentMode(metadata.mode)) {
      attachedSessions.delete(sessionId);
      return;
    }
    await webClient.sendFireAndForget(
      'chat.send',
      {
        session_id: sessionId,
        query: '',
        mode: metadata.mode,
        attach_only: true,
      },
      { isStream: true }
    );
  } catch (error) {
    if (generation === attachmentGeneration) attachedSessions.delete(sessionId);
    throw error;
  }
}
