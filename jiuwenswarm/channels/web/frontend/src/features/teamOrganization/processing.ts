// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useSyncExternalStore } from 'react';
import { useChatStore } from '../../stores/chatStore';
import { ownerTeamId } from './ownerIdentity';
import { useTeamOrganizationUiEnabled } from './featureConfig';

const activeRequests = new Map<string, Map<string, string>>();
const stoppedOwners = new Set<string>();
const pendingOwnerStops = new Set<string>();
const ownerStateRevisions = new Map<string, number>();
const listeners = new Set<() => void>();
let revision = 0;

function closeIdleOutput(key: string): void {
  const chat = useChatStore.getState();
  if (chat.getRuntime(key)?.isProcessing) return;
  chat.setThinking(key, false);
  chat.closeReasoning(key);
  chat.stopStreaming(key);
  for (const message of chat.getRuntime(key)?.messages ?? []) {
    if (message.isStreaming) chat.updateMessage(key, message.id, { isStreaming: false });
  }
}

function publish(key: string): void {
  revision += 1;
  const separator = key.indexOf('::');
  const sessionId = separator < 0 ? key : key.slice(0, separator);
  const chat = useChatStore.getState();
  const busy = Boolean(activeRequests.get(key)?.size);
  if (separator >= 0) {
    if (busy) chat.ensureTeamRuntime(sessionId, key.slice(separator + 2));
    chat.setProcessing(key, busy);
  }
  // Keep every store reader consistent without allowing either lifecycle to
  // clear the other. Direct expert chat does not make the whole organization busy.
  const organizationBusy = isOrganizationProcessing(sessionId);
  if (organizationBusy) chat.ensureRuntime(sessionId);
  chat.setOrganizationProcessing(sessionId, organizationBusy);
  closeIdleOutput(key);
  if (key !== sessionId) closeIdleOutput(sessionId);
  listeners.forEach((listener) => listener());
}

export function expertProcessingSnapshot(): { revision: number; keys: string[] } {
  return { revision, keys: [...new Set([...activeRequests.keys(), ...stoppedOwners])] };
}

export function replaceExpertRequests(key: string, requestIds: string[]): void {
  const previous = activeRequests.get(key);
  const requests = new Map(requestIds.filter(Boolean).map((id) => [id, previous?.get(id) ?? 'org_expert_direct']));
  if (requests.size) activeRequests.set(key, requests);
  else activeRequests.delete(key);
  publish(key);
}

export function organizationProcessingRevision(): number {
  return revision;
}

export function isOrganizationProcessing(sessionId: string): boolean {
  if (stoppedOwners.has(sessionId)) return false;
  for (const [key, requests] of activeRequests) {
    if (key !== sessionId && !key.startsWith(`${sessionId}::`)) continue;
    if ([...requests.values()].some((source) => source !== 'org_expert_direct')) return true;
  }
  return false;
}

export function isOrganizationOwnerStopped(sessionId: string): boolean {
  return stoppedOwners.has(sessionId);
}

export function organizationOwnerStateRevision(sessionId: string): number {
  return ownerStateRevisions.get(sessionId) ?? 0;
}

export function setOrganizationOwnerStopped(sessionId: string, stopped: boolean, pending = false): void {
  ownerStateRevisions.set(sessionId, organizationOwnerStateRevision(sessionId) + 1);
  if (pending) pendingOwnerStops.add(sessionId);
  else pendingOwnerStops.delete(sessionId);
  if (stopped) {
    stoppedOwners.add(sessionId);
    // Keep in-flight Owner rounds until acknowledgement so a rejected stop can restore them.
    if (!pending) activeRequests.delete(sessionId);
    useChatStore.getState().setProcessing(sessionId, false);
  } else {
    stoppedOwners.delete(sessionId);
  }
  publish(sessionId);
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
  if (!requestId || stoppedOwners.has(key)) return;
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

export interface ExpertRequest {
  team_id: string;
  request_id: string;
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
  expertRequests?: ExpertRequest[],
  ownerStopped?: boolean,
): void {
  if (revision !== expectedRevision) return;
  if (ownerStopped !== undefined && !pendingOwnerStops.has(sessionId)) {
    if (stoppedOwners.has(sessionId) !== ownerStopped) {
      ownerStateRevisions.set(sessionId, organizationOwnerStateRevision(sessionId) + 1);
    }
    if (ownerStopped) stoppedOwners.add(sessionId);
    else stoppedOwners.delete(sessionId);
  }
  const resolvedOwner = ownerId || ownerTeamId(sessionId);
  const keys = new Set([...activeRequests.keys()].filter((key) => key === sessionId || key.startsWith(`${sessionId}::`)));
  if (ownerStopped !== undefined) keys.add(sessionId);
  const incoming = new Map<string, Map<string, string>>();
  if (!Array.isArray(expertRequests)) {
    // An organization-only snapshot cannot declare direct expert requests idle.
    for (const key of keys) {
      const direct = new Map([...(activeRequests.get(key) ?? [])]
        .filter(([, source]) => source === 'org_expert_direct'));
      if (direct.size) incoming.set(key, direct);
    }
  }
  const directRequests = (expertRequests ?? []).map((request) => ({
    ...request, source: 'org_expert_direct',
  }));
  for (const request of [...directRequests, ...requests]) {
    if (!request.team_id || !request.request_id || !request.source?.startsWith('org_')) continue;
    const key = request.team_id === resolvedOwner ? sessionId : `${sessionId}::${request.team_id}`;
    if (key === sessionId && stoppedOwners.has(sessionId)) continue;
    const rounds = incoming.get(key) ?? new Map<string, string>();
    rounds.set(request.request_id, request.source);
    incoming.set(key, rounds);
    keys.add(key);
  }
  for (const key of keys) {
    const rounds = incoming.get(key);
    if (rounds?.size) activeRequests.set(key, rounds);
    else activeRequests.delete(key);
  }
  // Install the whole snapshot before publishing its aggregate busy state.
  for (const key of keys) publish(key);
}

export function clearOrganizationRequests(sessionId: string): void {
  ownerStateRevisions.set(sessionId, organizationOwnerStateRevision(sessionId) + 1);
  pendingOwnerStops.delete(sessionId);
  stoppedOwners.delete(sessionId);
  reconcileOrganizationRequests(sessionId, undefined, [], revision, []);
}
