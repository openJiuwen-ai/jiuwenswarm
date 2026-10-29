/**
 * WebUIRuntime：实例状态的唯一管理者。
 * 负责实例绑定、revision 比较、clear/error、挂载所有权、当前帧校验与显示反馈。
 * 不依赖 React；内容变化只通知对应实例，实例增删只通知对应挂载区。
 */

import type { WebUIInteractionResult } from '../contracts/interaction.ts';
import { webUIInstanceKey, webUISlotKey } from '../contracts/normalized.ts';
import type {
  WebUINormalizedClearFrame,
  WebUINormalizedErrorFrame,
  WebUINormalizedNode,
  WebUINormalizedRenderFrame,
  WebUINormalizedTarget,
} from '../contracts/normalized.ts';
import { createProtocolPipeline } from '../protocol/process.ts';
import type { WebUIProtocol } from '../protocol/process.ts';
import type { WebUIConnectionState, WebUITransportHandlers } from '../transport/types.ts';
import type {
  WebUIInstanceDispatch,
  WebUIInstanceSnapshot,
  WebUIMountHandle,
  WebUIMountOptions,
  WebUIRuntime,
  WebUIRuntimeOptions,
  WebUIScopeSnapshot,
} from './types.ts';

interface InstanceRecord {
  key: string;
  target: WebUINormalizedTarget;
  lastRenderRevision: number;
  snapshot: WebUIInstanceSnapshot;
}

interface MountedOwner {
  id: number;
  sessionId: string;
  slotId: string;
  instanceKey?: string;
  /** 本消费者上报过就绪的 instanceKey；重连时按消费者重发。 */
  ready: Set<string>;
}

type Listener = () => void;

export function createWebUIRuntime(options: WebUIRuntimeOptions): WebUIRuntime {
  const { transport, catalog } = options;
  const protocol: WebUIProtocol = options.protocol ?? createProtocolPipeline(catalog, options.limits);

  const records = new Map<string, InstanceRecord>();
  /** 挂载区 -> 实例键集合，范围订阅只关心成员变化。 */
  const scopeIndex = new Map<string, Set<string>>();
  const scopeOwners = new Map<string, Set<number>>();
  const owners = new Map<number, MountedOwner>();

  const globalListeners = new Set<Listener>();
  const instanceListeners = new Map<string, Set<Listener>>();
  const scopeListeners = new Map<string, Set<Listener>>();
  const instanceVersions = new Map<string, number>();
  const scopeVersions = new Map<string, number>();

  const inFlight = new Set<string>();
  let ownerCounter = 0;
  let localFrameCounter = 0;
  let version = 0;
  let connectionState: WebUIConnectionState = transport.getConnectionState();
  let unsubscribeTransport: (() => void) | null = null;
  let started = false;
  let disposed = false;

  function notifyListeners(listeners: Set<Listener> | undefined): void {
    if (!listeners) {
      return;
    }
    for (const listener of listeners) {
      listener();
    }
  }

  function bumpInstance(instanceKey: string): void {
    instanceVersions.set(instanceKey, (instanceVersions.get(instanceKey) ?? 0) + 1);
  }

  function bumpScope(scopeKey: string): void {
    scopeVersions.set(scopeKey, (scopeVersions.get(scopeKey) ?? 0) + 1);
  }

  function notifyInstance(instanceKey: string): void {
    version += 1;
    bumpInstance(instanceKey);
    notifyListeners(instanceListeners.get(instanceKey));
  }

  function notifyScope(scopeKey: string): void {
    version += 1;
    bumpScope(scopeKey);
    notifyListeners(scopeListeners.get(scopeKey));
  }

  function bumpAll(): void {
    version += 1;
    for (const key of records.keys()) {
      bumpInstance(key);
    }
    const scopeKeys = new Set<string>([
      ...scopeVersions.keys(),
      ...scopeListeners.keys(),
      ...scopeIndex.keys(),
      ...scopeOwners.keys(),
    ]);
    for (const key of scopeKeys) {
      bumpScope(key);
    }
  }

  function notifyAll(): void {
    bumpAll();
    notifyListeners(globalListeners);
    for (const listeners of instanceListeners.values()) {
      notifyListeners(listeners);
    }
    for (const listeners of scopeListeners.values()) {
      notifyListeners(listeners);
    }
  }

  function indexInstance(target: WebUINormalizedTarget): void {
    const scopeKey = webUISlotKey(target.sessionId, target.slotId);
    let keys = scopeIndex.get(scopeKey);
    if (!keys) {
      keys = new Set<string>();
      scopeIndex.set(scopeKey, keys);
    }
    keys.add(webUIInstanceKey(target));
  }

  function unindexInstance(target: WebUINormalizedTarget): void {
    const scopeKey = webUISlotKey(target.sessionId, target.slotId);
    const keys = scopeIndex.get(scopeKey);
    if (!keys) {
      return;
    }
    keys.delete(webUIInstanceKey(target));
    if (keys.size === 0) {
      scopeIndex.delete(scopeKey);
    }
  }

  function buildSnapshot(
    target: WebUINormalizedTarget,
    state: 'active' | 'error',
    revision: number,
    frame: WebUINormalizedRenderFrame | null,
    error: WebUIInstanceSnapshot['error'],
    displayedRevision = 0,
  ): WebUIInstanceSnapshot {
    return {
      key: webUIInstanceKey(target),
      target,
      state,
      revision,
      frame,
      error,
      connected: connectionState === 'connected',
      displayedRevision,
      renderFailure: null,
      localFrameId: ++localFrameCounter,
    };
  }

  function commitRenderFrame(frame: WebUINormalizedRenderFrame): void {
    const key = webUIInstanceKey(frame.target);
    const existing = records.get(key);
    if (existing && frame.revision <= existing.lastRenderRevision) {
      return;
    }
    const snapshot = buildSnapshot(
      frame.target,
      'active',
      frame.revision,
      frame,
      null,
      existing?.snapshot.displayedRevision ?? 0,
    );
    if (existing) {
      existing.target = frame.target;
      existing.lastRenderRevision = frame.revision;
      existing.snapshot = snapshot;
    } else {
      records.set(key, { key, target: frame.target, lastRenderRevision: frame.revision, snapshot });
      indexInstance(frame.target);
      notifyScope(webUISlotKey(frame.target.sessionId, frame.target.slotId));
    }
    notifyInstance(key);
    if (existing && catalog.getSlot(frame.target.slotId)?.display === 'single') {
      notifyScope(webUISlotKey(frame.target.sessionId, frame.target.slotId));
    }
  }

  function commitClearFrame(frame: WebUINormalizedClearFrame): void {
    const key = webUIInstanceKey(frame.target);
    const existing = records.get(key);
    if (!existing) {
      return;
    }
    if (frame.revision < existing.lastRenderRevision) {
      return;
    }
    const target = existing.target;
    records.delete(key);
    unindexInstance(target);
    for (const ownerId of scopeOwners.get(webUISlotKey(target.sessionId, target.slotId)) ?? []) {
      owners.get(ownerId)?.ready.delete(target.instanceKey);
    }
    notifyScope(webUISlotKey(target.sessionId, target.slotId));
    notifyInstance(key);
  }

  function commitErrorFrame(frame: WebUINormalizedErrorFrame): void {
    const key = webUIInstanceKey(frame.target);
    const existing = records.get(key);
    const error = { code: frame.code, message: frame.message };
    if (existing) {
      // 有旧界面时保留并附带错误，不清空已显示内容。
      existing.target = frame.target;
      existing.snapshot = {
        ...existing.snapshot,
        target: frame.target,
        state: existing.snapshot.frame ? 'active' : 'error',
        error,
        connected: connectionState === 'connected',
      };
      notifyInstance(key);
      return;
    }
    records.set(key, {
      key,
      target: frame.target,
      lastRenderRevision: -1,
      snapshot: buildSnapshot(frame.target, 'error', frame.revision, null, error),
    });
    indexInstance(frame.target);
    notifyScope(webUISlotKey(frame.target.sessionId, frame.target.slotId));
    notifyInstance(key);
  }

  function refreshConnectedFlag(): void {
    const connected = connectionState === 'connected';
    for (const record of records.values()) {
      record.snapshot = { ...record.snapshot, connected };
    }
  }

  const transportHandlers: WebUITransportHandlers = {
    onRenderFrame(payload) {
      if (disposed) {
        return;
      }
      const result = protocol.processRenderFrame(payload);
      if (result.ok) {
        commitRenderFrame(result.value);
      }
    },
    onClearFrame(payload) {
      if (disposed) {
        return;
      }
      const result = protocol.processClearFrame(payload);
      if (result.ok) {
        commitClearFrame(result.value);
      }
    },
    onErrorFrame(payload) {
      if (disposed) {
        return;
      }
      const result = protocol.processErrorFrame(payload);
      if (result.ok) {
        commitErrorFrame(result.value);
      }
    },
    onConnectionStateChange(state) {
      if (disposed || state === connectionState) {
        return;
      }
      connectionState = state;
      refreshConnectedFlag();
      notifyAll();
      if (state === 'connected') {
        void reportReadyAfterReconnect();
      }
    },
  };

  async function sendReady(owner: MountedOwner, instanceKey: string): Promise<void> {
    if (disposed || connectionState !== 'connected') {
      return;
    }
    try {
      await transport.dispatch({
        event: 'ui.ready',
        target: { sessionId: owner.sessionId, slotId: owner.slotId, packageId: '', instanceKey },
        payload: {},
      });
    } catch {
      // 就绪上报失败不影响挂载；断连后重连时会重发。
    }
  }

  async function reportReadyAfterReconnect(): Promise<void> {
    const tasks: Array<Promise<void>> = [];
    for (const owner of owners.values()) {
      if (owner.ready.size === 0) {
        tasks.push(sendReady(owner, ''));
        continue;
      }
      for (const instanceKey of owner.ready) {
        tasks.push(sendReady(owner, instanceKey));
      }
    }
    await Promise.all(tasks);
  }

  function mountSlot(sessionId: string, slotId: string, mountOptions: WebUIMountOptions = {}): WebUIMountHandle {
    if (disposed) {
      throw new Error('WebUI runtime is disposed');
    }
    if (!catalog.getSlot(slotId)) {
      throw new Error(`WebUI slot is not registered: ${slotId}`);
    }
    ownerCounter += 1;
    const owner: MountedOwner = {
      id: ownerCounter,
      sessionId,
      slotId,
      instanceKey: mountOptions.instanceKey,
      ready: new Set<string>(),
    };
    owners.set(owner.id, owner);
    const scopeKey = webUISlotKey(sessionId, slotId);
    let scopeOwnerIds = scopeOwners.get(scopeKey);
    if (!scopeOwnerIds) {
      scopeOwnerIds = new Set<number>();
      scopeOwners.set(scopeKey, scopeOwnerIds);
    }
    scopeOwnerIds.add(owner.id);
    notifyScope(scopeKey);

    let released = false;
    const handle: WebUIMountHandle = {
      sessionId,
      slotId,
      instanceKey: mountOptions.instanceKey,
      async notifyReady() {
        if (disposed || released) {
          return;
        }
        const readyKey = owner.instanceKey ?? '';
        owner.ready.add(readyKey);
        await sendReady(owner, readyKey);
      },
      release() {
        if (released) {
          return;
        }
        released = true;
        owners.delete(owner.id);
        const currentOwnerIds = scopeOwners.get(scopeKey);
        currentOwnerIds?.delete(owner.id);
        if (currentOwnerIds && currentOwnerIds.size > 0) {
          return;
        }
        // 最后一个消费者退出：移除该范围不再被持有的本地实例。
        scopeOwners.delete(scopeKey);
        const instanceKeys = scopeIndex.get(scopeKey);
        if (instanceKeys) {
          for (const instanceKey of [...instanceKeys]) {
            const record = records.get(instanceKey);
            if (!record) {
              continue;
            }
            records.delete(instanceKey);
            unindexInstance(record.target);
            notifyInstance(instanceKey);
          }
        }
        notifyScope(scopeKey);
      },
    };
    return handle;
  }

  async function dispatchIntent(request: WebUIInstanceDispatch): Promise<WebUIInteractionResult> {
    const record = records.get(request.instanceKey);
    const frame = record?.snapshot.frame;
    if (!record || !frame || frame.revision !== request.frameRevision) {
      return { status: 'inactive' };
    }
    if (connectionState !== 'connected') {
      return { status: 'disconnected' };
    }
    const node = findNode(frame.tree, request.nodeKey);
    if (!node || node.action === null || node.action.id !== request.actionId) {
      return { status: 'invalid_action' };
    }
    const rule = catalog.getComponent(node.component);
    if (!rule || !rule.actionEvent) {
      return { status: 'unsupported_action' };
    }

    const args = request.args ?? node.action.args;
    const dedupeKey = `${request.instanceKey}|${request.nodeKey}|${request.actionId}|${JSON.stringify(args)}`;
    if (inFlight.has(dedupeKey)) {
      return { status: 'duplicate' };
    }
    inFlight.add(dedupeKey);
    try {
      const payload: Record<string, unknown> = { ...(request.payload ?? {}) };
      payload.action_id = request.actionId;
      payload.args = args;
      const receipt = await transport.dispatch({
        event: rule.actionEvent,
        target: record.target,
        payload,
      });
      return { status: 'sent', receipt };
    } catch (error) {
      return { status: 'failed', error: error instanceof Error ? error.message : String(error) };
    } finally {
      inFlight.delete(dedupeKey);
    }
  }

  function reportDisplayed(instanceKey: string, revision: number): void {
    const record = records.get(instanceKey);
    if (!record || record.snapshot.frame?.revision !== revision) {
      return;
    }
    if (record.snapshot.displayedRevision >= revision) {
      return;
    }
    record.snapshot = { ...record.snapshot, displayedRevision: revision };
    notifyInstance(instanceKey);
  }

  function reportRenderFailure(instanceKey: string, revision: number, message: string): void {
    const record = records.get(instanceKey);
    if (!record || record.snapshot.frame?.revision !== revision) {
      return;
    }
    record.snapshot = { ...record.snapshot, renderFailure: { revision, message } };
    notifyInstance(instanceKey);
  }

  function clearSession(sessionId: string): void {
    const changedScopes = new Set<string>();
    for (const [key, record] of records) {
      if (record.target.sessionId !== sessionId) {
        continue;
      }
      changedScopes.add(webUISlotKey(record.target.sessionId, record.target.slotId));
      records.delete(key);
      unindexInstance(record.target);
      notifyInstance(key);
    }
    for (const [ownerId, owner] of owners) {
      if (owner.sessionId !== sessionId) {
        continue;
      }
      changedScopes.add(webUISlotKey(owner.sessionId, owner.slotId));
      owners.delete(ownerId);
      scopeOwners.get(webUISlotKey(owner.sessionId, owner.slotId))?.delete(ownerId);
    }
    for (const scopeKey of changedScopes) {
      if ((scopeOwners.get(scopeKey)?.size ?? 0) === 0) {
        scopeOwners.delete(scopeKey);
      }
      notifyScope(scopeKey);
    }
  }

  function getScopeSnapshot(sessionId: string, slotId: string): WebUIScopeSnapshot {
    const scopeKey = webUISlotKey(sessionId, slotId);
    return {
      sessionId,
      slotId,
      mounted: (scopeOwners.get(scopeKey)?.size ?? 0) > 0,
      connected: connectionState === 'connected',
      instanceKeys: [...(scopeIndex.get(scopeKey) ?? [])],
    };
  }

  function start(): void {
    if (disposed) {
      throw new Error('WebUI runtime is disposed');
    }
    if (started) {
      return;
    }
    started = true;
    connectionState = transport.getConnectionState();
    unsubscribeTransport = transport.subscribe(transportHandlers);
    notifyAll();
  }

  function dispose(): void {
    if (disposed) {
      return;
    }
    disposed = true;
    started = false;
    if (unsubscribeTransport) {
      unsubscribeTransport();
      unsubscribeTransport = null;
    }
    records.clear();
    scopeIndex.clear();
    scopeOwners.clear();
    owners.clear();
    instanceListeners.clear();
    scopeListeners.clear();
    instanceVersions.clear();
    scopeVersions.clear();
    globalListeners.clear();
    inFlight.clear();
    version += 1;
  }

  function subscribe(listener: Listener): () => void {
    globalListeners.add(listener);
    return () => {
      globalListeners.delete(listener);
    };
  }

  function addScopedListener(map: Map<string, Set<Listener>>, key: string, listener: Listener): () => void {
    let listeners = map.get(key);
    if (!listeners) {
      listeners = new Set<Listener>();
      map.set(key, listeners);
    }
    listeners.add(listener);
    return () => {
      const current = map.get(key);
      if (!current) {
        return;
      }
      current.delete(listener);
      if (current.size === 0) {
        map.delete(key);
      }
    };
  }

  return {
    start,
    dispose,
    getCatalog: () => catalog,
    subscribe,
    getVersion: () => version,
    subscribeInstance: (instanceKey, listener) => addScopedListener(instanceListeners, instanceKey, listener),
    getInstanceVersion: (instanceKey) => instanceVersions.get(instanceKey) ?? 0,
    getInstance: (instanceKey) => records.get(instanceKey)?.snapshot,
    subscribeScope: (sessionId, slotId, listener) =>
      addScopedListener(scopeListeners, webUISlotKey(sessionId, slotId), listener),
    getScopeVersion: (sessionId, slotId) => scopeVersions.get(webUISlotKey(sessionId, slotId)) ?? 0,
    getScopeSnapshot,
    mountSlot,
    dispatchIntent,
    reportDisplayed,
    reportRenderFailure,
    clearSession,
    getConnectionState: () => connectionState,
  };
}

function findNode(tree: WebUINormalizedNode, key: string): WebUINormalizedNode | null {
  if (tree.key === key) {
    return tree;
  }
  for (const child of tree.children) {
    const found = findNode(child, key);
    if (found) {
      return found;
    }
  }
  return null;
}
