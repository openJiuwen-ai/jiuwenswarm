// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useChatStore } from '../../stores/chatStore';

const activeRequests = new Map<string, Set<string>>();
let revision = 0;

export function expertProcessingSnapshot(): { revision: number; keys: string[] } {
  return { revision, keys: [...activeRequests.keys()] };
}

export function replaceExpertRequests(key: string, requestIds: string[]): void {
  revision += 1;
  const requests = new Set(requestIds.filter(Boolean));
  if (requests.size) activeRequests.set(key, requests);
  else activeRequests.delete(key);
  const chat = useChatStore.getState();
  chat.setProcessing(key, requests.size > 0);
  if (!requests.size) {
    chat.setThinking(key, false);
    chat.closeReasoning(key);
    chat.stopStreaming(key);
  }
}

export function startExpertRequest(key: string, requestId: string): void {
  if (!requestId) return;
  revision += 1;
  const requests = activeRequests.get(key) ?? new Set<string>();
  requests.add(requestId);
  activeRequests.set(key, requests);
  useChatStore.getState().setProcessing(key, true);
}

/** An old or unrelated terminal must leave all newer requests running. */
export function finishExpertRequest(key: string, requestId: string): boolean {
  revision += 1;
  const requests = activeRequests.get(key);
  requests?.delete(requestId);
  const idle = !requests?.size;
  if (idle) activeRequests.delete(key);
  useChatStore.getState().setProcessing(key, !idle);
  return idle;
}
