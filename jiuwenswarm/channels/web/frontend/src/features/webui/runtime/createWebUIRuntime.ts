/**
 * WebUIRuntime：实例状态的唯一管理者。
 * 负责实例绑定、revision 比较、clear/error、挂载所有权、当前帧校验与显示反馈。
 * 不依赖 React；内容变化只通知对应实例，实例增删只通知对应挂载区。
 */

import type { WebUIInteractionResult } from '../contracts/interaction.ts';
import { findNormalizedNode, webUIInstanceKey, webUISlotKey } from '../contracts/normalized.ts';
import type { WebUIComponentEvent } from '../contracts/wire.ts';
import { checkPropValue } from '../protocol/checkPropValue.ts';
import { DEFAULT_WEBUI_LIMITS } from '../protocol/limits.ts';
import { isJSONData, isRecord, jsonByteLength } from '../protocol/utils.ts';
import type {
  WebUINormalizedClearFrame,
  WebUINormalizedErrorFrame,
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

interface MountedOwner extends WebUIMountOptions {
  id: number;
  sessionId: string;
  slotId: string;
  instanceKey?: string;
  packageId?: string;
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

  const nodeBindings = new Map<number, { instanceKey: string; localFrameId: number; nodeKey: string; ownerIds: number[] }>();
  let localFrameCounter = 0;
  let nodeLeaseCounter = 0;
  let ownerCounter = 0;
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
      lifetimeId: localFrameCounter,
      displayedLocalFrameId: null,
    };
  }

  function commitRenderFrame(frame: WebUINormalizedRenderFrame): void {
    const key = webUIInstanceKey(frame.target);
    const existing = records.get(key);
    if (existing && frame.revision <= existing.lastRenderRevision) {
      return;
    }
    invalidateBindings(key);
    const snapshot = buildSnapshot(
      frame.target,
      'active',
      frame.revision,
      frame,
      null,
      existing?.snapshot.displayedRevision ?? 0,
    );
    if (existing) {
      snapshot.lifetimeId = existing.snapshot.lifetimeId;
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
    if (existing && frame.revision < existing.lastRenderRevision) {
      return;
    }
    if (!existing) return;
    const target = existing.target;
    invalidateBindings(key);
    records.delete(key);
    unindexInstance(target);
    notifyScope(webUISlotKey(target.sessionId, target.slotId));
    notifyInstance(key);
  }

  function commitErrorFrame(frame: WebUINormalizedErrorFrame): void {
    const key = webUIInstanceKey(frame.target);
    const existing = records.get(key);
    const error = { code: frame.code, message: frame.message };
    if (existing) {
      if (frame.revision < existing.lastRenderRevision) return;
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
      if (state !== 'connected') {
        nodeBindings.clear();
      }
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
        target: { sessionId: owner.sessionId, slotId: owner.slotId, packageId: owner.packageId ?? '', instanceKey },
        payload: {},
      });
    } catch {
      // Ready failures do not affect mounting; reconnect retries the request.
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
      ...mountOptions,
      id: ownerCounter,
      sessionId,
      slotId,
      instanceKey: mountOptions.instanceKey,
      packageId: mountOptions.packageId,
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
      mountLeaseId: owner.id,
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
        for (const [id, binding] of nodeBindings) {
          if (!binding.ownerIds.some((ownerId) => owners.has(ownerId))) nodeBindings.delete(id);
        }
        const currentOwnerIds = scopeOwners.get(scopeKey);
        currentOwnerIds?.delete(owner.id);
        if (currentOwnerIds && currentOwnerIds.size > 0) {
          for (const key of [...(scopeIndex.get(scopeKey) ?? [])]) {
            const record = records.get(key);
            if (!record) continue;
            const held = [...owners.values()].some((remaining) => remaining.sessionId === record.target.sessionId
              && remaining.slotId === record.target.slotId
              && (remaining.instanceKey === undefined || remaining.instanceKey === record.target.instanceKey)
              && (remaining.packageId === undefined || remaining.packageId === record.target.packageId));
            if (!held) {
              invalidateBindings(key); records.delete(key); unindexInstance(record.target); notifyInstance(key);
            }
          }
          notifyScope(scopeKey);
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
    return dispatchComponentEvent(request, record, frame);
  }

  function invalidateBindings(instanceKey: string): void {
    for (const [id, binding] of nodeBindings) {
      if (binding.instanceKey === instanceKey) nodeBindings.delete(id);
    }
  }

  function bindNode(instanceKey: string, localFrameId: number, nodeKey: string, mountLeaseId?: number) {
    const id = ++nodeLeaseCounter;
    const record = records.get(instanceKey);
    const ownerIds = record ? [...owners.values()].filter((owner) => owner.sessionId === record.target.sessionId
      && owner.slotId === record.target.slotId
      && (owner.instanceKey === undefined || owner.instanceKey === record.target.instanceKey)
      && (owner.packageId === undefined || owner.packageId === record.target.packageId)
      && (mountLeaseId === undefined || owner.id === mountLeaseId)).map((owner) => owner.id).slice(0, 1) : [];
    if (!disposed && record?.snapshot.localFrameId === localFrameId && connectionState === 'connected' && ownerIds.length) {
      nodeBindings.set(id, { instanceKey, localFrameId, nodeKey, ownerIds });
    }
    return {
      emit: (event: string, data: Record<string, unknown>) => dispatchIntent({
        instanceKey, localFrameId, frameRevision: record?.snapshot.frame?.revision ?? -1,
        nodeKey, nodeLeaseId: id, event, data,
      }),
      release: () => { nodeBindings.delete(id); },
    };
  }

  async function dispatchComponentEvent(
    request: WebUIInstanceDispatch,
    record: InstanceRecord,
    frame: WebUINormalizedRenderFrame,
  ): Promise<WebUIInteractionResult> {
    const binding = request.nodeLeaseId === undefined ? undefined : nodeBindings.get(request.nodeLeaseId);
    if (disposed || !binding || binding.instanceKey !== request.instanceKey || binding.nodeKey !== request.nodeKey
      || binding.localFrameId !== record.snapshot.localFrameId || request.localFrameId !== record.snapshot.localFrameId
      || !binding.ownerIds.some((id) => owners.has(id))
      || record.snapshot.displayedLocalFrameId !== record.snapshot.localFrameId || record.snapshot.renderFailure) return { status: 'inactive' };
    const node = findNormalizedNode(frame.tree, request.nodeKey);
    if (!node || node.props.disabled === true || node.props.loading === true) return { status: 'inactive' };
    const event = request.event ?? '';
    if (node.component === 'Input' && node.props.readOnly === true && event !== 'ui.click') return { status: 'inactive' };
    const rule = catalog.getComponent(node.component);
    const dataRule = rule?.events?.[event];
    const action = node.events?.[event];
    if (!dataRule || !action || !Object.prototype.hasOwnProperty.call(rule?.events ?? {}, event)
      || !Object.prototype.hasOwnProperty.call(node.events ?? {}, event)) return { status: 'invalid_action' };
    const data = request.data;
    const limits = options.limits ?? DEFAULT_WEBUI_LIMITS;
    if (!isRecord(data) || !isJSONData(data) || checkPropValue(dataRule, data, limits)) {
      return { status: 'invalid_action' };
    }
    if (rule?.validateEvent?.(event, data, node.props)) return { status: 'invalid_action' };
    const payload = {
      action_id: action.id, args: action.args, data: JSON.parse(JSON.stringify(data)),
      node_key: request.nodeKey, revision: frame.revision,
    };
    if (jsonByteLength(payload) > limits.maxFrameBytes) return { status: 'invalid_action' };
    // Each callback is a separate action, including repeated equal values. No in-flight coalescing.
    try {
      const receipt = await transport.dispatch({
        event: event as WebUIComponentEvent, target: record.target,
        payload,
      });
      if (!nodeBindings.has(request.nodeLeaseId!) || records.get(record.key)?.snapshot.localFrameId !== request.localFrameId) {
        return { status: 'stale' };
      }
      return { status: 'sent', receipt };
    } catch (error) {
      return { status: 'failed', error: error instanceof Error ? error.message : String(error) };
    }
  }

  function reportDisplayed(instanceKey: string, revision: number, localFrameId?: number): void {
    const record = records.get(instanceKey);
    if (!record || record.snapshot.frame?.revision !== revision) {
      return;
    }
    if (record.snapshot.localFrameId !== localFrameId || record.snapshot.renderFailure) return;
    record.snapshot = { ...record.snapshot, displayedRevision: revision, displayedLocalFrameId: localFrameId };
  }

  function reportRenderFailure(instanceKey: string, revision: number, message: string, localFrameId?: number): void {
    const record = records.get(instanceKey);
    if (!record || record.snapshot.frame?.revision !== revision) {
      return;
    }
    if (record.snapshot.localFrameId !== localFrameId) return;
    invalidateBindings(instanceKey);
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
      invalidateBindings(key);
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
    nodeBindings.clear();
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
    bindNode,
    reportDisplayed,
    reportRenderFailure,
    clearSession,
    getConnectionState: () => connectionState,
  };
}
