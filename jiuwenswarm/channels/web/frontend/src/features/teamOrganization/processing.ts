// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useSyncExternalStore } from 'react';
import { useChatStore } from '../../stores/chatStore';
import { useTeamOrganizationUiEnabled } from './featureConfig';

const activeRequests = new Map<string, Map<string, string>>();
const listeners = new Set<() => void>();
let revision = 0;

function publish(key: string): void {
  revision += 1;
  // The foreground Owner has a separate lifecycle. Aggregate its background
  // work in the view without overwriting a foreground processing notification.
  const separator = key.indexOf('::');
  const chat = useChatStore.getState();
  const busy = Boolean(activeRequests.get(key)?.size);
  if (separator >= 0) {
    if (busy) chat.ensureTeamRuntime(key.slice(0, separator), key.slice(separator + 2));
    chat.setProcessing(key, busy);
  }
  // A background Owner terminal may close its output only when no foreground
  // Owner turn is processing. Never clear an independently running foreground.
  if (!busy && (separator >= 0 || !chat.getRuntime(key)?.isProcessing)) {
    chat.setThinking(key, false);
    chat.closeReasoning(key);
    chat.stopStreaming(key);
    for (const message of chat.getRuntime(key)?.messages ?? []) {
      if (message.isStreaming) chat.updateMessage(key, message.id, { isStreaming: false });
    }
  }
  listeners.forEach((listener) => listener());
}

export function expertProcessingSnapshot(): { revision: number; keys: string[] } {
  return { revision, keys: [...activeRequests.keys()] };
}

export function replaceExpertRequests(key: string, requestIds: string[]): void {
  const previous = activeRequests.get(key);
  const requests = new Map(requestIds.filter(Boolean).map((id) => [id, previous?.get(id) ?? 'org_expert_direct']));
  if (requests.size) activeRequests.set(key, requests);
  else activeRequests.delete(key);
  publish(key);
  if (!requests.size && key.includes('::')) {
    const chat = useChatStore.getState();
    chat.setThinking(key, false);
    chat.closeReasoning(key);
    chat.stopStreaming(key);
  }
}

export function organizationProcessingRevision(): number {
  return revision;
}

export function isOrganizationProcessing(sessionId: string): boolean {
  for (const [key, requests] of activeRequests) {
    if (key !== sessionId && !key.startsWith(`${sessionId}::`)) continue;
    if ([...requests.values()].some((source) => source !== 'org_expert_direct')) return true;
  }
  return false;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useOrganizationProcessing(sessionId: string | null): boolean {
  const enabled = useTeamOrganizationUiEnabled();
  return useSyncExternalStore(
    subscribe,
    () => enabled && Boolean(sessionId && isOrganizationProcessing(sessionId)),
    () => false,
  );
}

export function startExpertRequest(key: string, requestId: string, source = 'org_expert_direct'): void {
  if (!requestId) return;
  const requests = activeRequests.get(key) ?? new Map<string, string>();
  requests.set(requestId, source);
  activeRequests.set(key, requests);
  publish(key);
}

/** An old or unrelated terminal must leave all newer requests running. */
export function finishExpertRequest(key: string, requestId: string): boolean {
  revision += 1;
  const requests = activeRequests.get(key);
  requests?.delete(requestId);
  const idle = !requests?.size;
  if (idle) activeRequests.delete(key);
  publish(key);
  return idle;
}

export interface OrganizationRequest {
  team_id: string;
  request_id: string;
  source: string;
}

/** A send/terminal received during the query takes precedence over its snapshot. */
export function reconcileOrganizationRequests(
  sessionId: string,
  ownerId: string | undefined,
  requests: OrganizationRequest[],
  expectedRevision: number,
): void {
  if (revision !== expectedRevision) return;
  const keys = new Set([...activeRequests.keys()].filter((key) => key === sessionId || key.startsWith(`${sessionId}::`)));
  const incoming = new Map<string, Map<string, string>>();
  for (const request of requests) {
    if (!request.team_id || !request.request_id || !request.source?.startsWith('org_')) continue;
    const key = request.team_id === ownerId ? sessionId : `${sessionId}::${request.team_id}`;
    const rounds = incoming.get(key) ?? new Map<string, string>();
    rounds.set(request.request_id, request.source);
    incoming.set(key, rounds);
    keys.add(key);
  }
  for (const key of keys) {
    const rounds = incoming.get(key);
    if (rounds?.size) activeRequests.set(key, rounds);
    else activeRequests.delete(key);
    publish(key);
    if (!rounds?.size && key.includes('::')) {
      const chat = useChatStore.getState();
      chat.closeReasoning(key);
      chat.setThinking(key, false);
      chat.stopStreaming(key);
    }
  }
}

export function clearOrganizationRequests(sessionId: string): void {
  reconcileOrganizationRequests(sessionId, undefined, [], revision);
}
