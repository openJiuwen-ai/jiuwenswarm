import { isPluginResourceURL } from '../resources/urls.ts';
import { isRecord } from '../protocol/utils.ts';
import type { ClientContextChange, ClientDeclarationSnapshot, ClientOperation, RequestBinding } from './types.ts';

const PREFIX = 'workswarm.client.local_storage:';
const FORBIDDEN = new Set(['__proto__', 'prototype', 'constructor']);
const HEADER_NAME = /^[!#$%&'*+.^_`|~\da-z-]+$/i;
const CONTROL_VALUE = /[\x00-\x1f\x7f]/;
const bytes = (value: string) => new TextEncoder().encode(value).length;
const nonEmpty = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0;
const plain = (value: unknown): value is Record<string, unknown> => isRecord(value)
  && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);

export class ClientCapabilityError extends Error {
  constructor(readonly code: string) { super(code); }
}
function requireValue(condition: unknown, code = 'SCHEMA_ERROR'): asserts condition {
  if (!condition) throw new ClientCapabilityError(code);
}
function keys(value: Record<string, unknown>, allowed: string[]) {
  requireValue(Object.keys(value).every((key) => allowed.includes(key)));
}
function path(value: unknown): value is string[] {
  return Array.isArray(value) && value.length <= 8
    && value.every((part) => nonEmpty(part) && !FORBIDDEN.has(part));
}
function validateJSON(value: unknown, depth = 0, parents = new Set<object>()): void {
  requireValue(depth <= 8);
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return;
  if (typeof value === 'number') { requireValue(Number.isFinite(value)); return; }
  requireValue(Array.isArray(value) || plain(value));
  requireValue(!parents.has(value));
  parents.add(value);
  for (const item of Object.values(value)) validateJSON(item, depth + 1, parents);
  parents.delete(value);
}
function validateBinding(raw: unknown): asserts raw is RequestBinding {
  requireValue(plain(raw)); keys(raw, ['type', 'source', 'target']);
  requireValue(plain(raw.source) && plain(raw.target));
  keys(raw.source, ['key', 'path']);
  requireValue(nonEmpty(raw.source.key) && bytes(raw.source.key) <= 128);
  requireValue(raw.source.path === undefined || path(raw.source.path));
  requireValue(raw.target.prefix === undefined || typeof raw.target.prefix === 'string');
  if (raw.type === 'http') {
    keys(raw.target, ['location', 'name', 'prefix']);
    requireValue(raw.target.location === 'header' && typeof raw.target.name === 'string'
      && HEADER_NAME.test(raw.target.name));
    requireValue(!['host', 'origin', 'cookie', 'connection', 'upgrade', 'content-length', 'referer', 'x-user-id']
      .includes(raw.target.name.toLowerCase()) && !/^(sec-|proxy-)/i.test(raw.target.name));
    requireValue(!CONTROL_VALUE.test((raw.target.prefix as string | undefined) ?? ''));
  } else {
    keys(raw.target, ['location', 'path', 'prefix']);
    requireValue(raw.type === 'websocket' && raw.target.location === 'message' && path(raw.target.path)
      && raw.target.path.length >= 2 && raw.target.path[0] === 'request_context');
  }
}

export interface ClientCapabilityHostOptions {
  storage: () => Storage;
  gatewayUrl: () => string;
  waitForInvocation?: (id: string) => Promise<void>;
  reloadPage?: () => void;
}

export function createClientCapabilityHost(options: ClientCapabilityHostOptions) {
  let declarations: ClientDeclarationSnapshot | null = null;
  let epoch = 0;
  let signature = '';
  let errorCode: string | null = null;
  let changing = false;
  let invalidConfiguration = false;
  let awaitingDeclarations = false;
  let controller = new AbortController();
  let waiter = options.waitForInvocation ?? (() => Promise.resolve());
  const changes = new Set<(change: ClientContextChange) => void>();
  const errors = new Set<() => void>();
  const storageListeners = new Set<(change: { package_id: string; key: string }) => void>();
  const keyFor = (packageId: string, key: string) => PREFIX + JSON.stringify([declarations!.gatewayInstanceId, packageId, key]);
  function reportError(code: string | null) { errorCode = code; for (const listener of errors) listener(); }
  const allBindings = () => (declarations?.packages ?? []).flatMap((pkg) => pkg.requestBindings.map((binding) => ({ pkg, binding })));

  function values() {
    const entries = allBindings();
    const sources = new Map<string, unknown>();
    return entries.map(({ pkg, binding }) => {
      const key = keyFor(pkg.packageId, binding.source.key);
      if (!sources.has(key)) {
        try {
          const raw = options.storage().getItem(key);
          sources.set(key, raw === null ? undefined : JSON.parse(raw));
        } catch { sources.set(key, undefined); }
      }
      let value = sources.get(key);
      for (const part of binding.source.path ?? []) {
        value = plain(value) && Object.prototype.hasOwnProperty.call(value, part) ? value[part] : undefined;
      }
      if (typeof value !== 'string') return undefined;
      const result = (binding.target.prefix ?? '') + value;
      if (binding.type === 'http' && (CONTROL_VALUE.test(result) || [...result].some((char) => char.charCodeAt(0) > 255))) {
        return undefined;
      }
      return result;
    });
  }
  function advance(invocationId?: string) {
    changing = true;
    controller.abort(); controller = new AbortController();
    const current = ++epoch;
    for (const listener of changes) listener({ epoch: current, phase: 'changing' });
    void (invocationId ? waiter(invocationId) : Promise.resolve()).then(() => {
      if (current !== epoch) return;
      changing = false;
      for (const listener of changes) listener({ epoch: current, phase: 'ready' });
    });
  }
  function capture(invocationId?: string) {
    const current = values();
    const next = JSON.stringify(current);
    if (signature !== next) { signature = next; advance(invocationId); }
    return current;
  }
  function isGateway(input: string | URL | Request) {
    const base = new URL(options.gatewayUrl());
    const url = new URL(input instanceof Request ? input.url : String(input), base);
    return ['http:', 'https:'].includes(url.protocol) && url.origin === base.origin
      && (base.pathname === '/' || url.pathname === base.pathname || url.pathname.startsWith(base.pathname.replace(/\/$/, '') + '/'));
  }
  function configure(input: unknown): void {
    try {
      requireValue(plain(input)); keys(input, ['clientProtocolVersion', 'gatewayInstanceId', 'connectionEpoch', 'packages']);
      requireValue(input.clientProtocolVersion === 1 && nonEmpty(input.gatewayInstanceId) && nonEmpty(input.connectionEpoch)
        && Array.isArray(input.packages));
      const packageIds = new Set<string>();
      const targets: { type: string; parts: string[] }[] = [];
      for (const pkg of input.packages) {
        requireValue(plain(pkg)); keys(pkg, ['packageId', 'activationGeneration', 'requestBindings']);
        requireValue(nonEmpty(pkg.packageId) && !packageIds.has(pkg.packageId) && nonEmpty(pkg.activationGeneration)
          && Array.isArray(pkg.requestBindings));
        packageIds.add(pkg.packageId);
        for (const binding of pkg.requestBindings) {
          validateBinding(binding);
          const parts = binding.target.location === 'header' ? [binding.target.name.toLowerCase()] : binding.target.path;
          requireValue(!targets.some((other) => other.type === binding.type
            && other.parts.slice(0, Math.min(parts.length, other.parts.length)).every((part, index) => part === parts[index])), 'BINDING_CONFLICT');
          targets.push({ type: binding.type, parts });
        }
      }
      declarations = JSON.parse(JSON.stringify(input)) as ClientDeclarationSnapshot;
      invalidConfiguration = false;
      awaitingDeclarations = false;
      signature = JSON.stringify(values());
    } catch (error) {
      declarations = null; signature = ''; advance();
      invalidConfiguration = true;
      awaitingDeclarations = false;
      reportError('CONFIGURATION_ERROR');
      throw new ClientCapabilityError(error instanceof ClientCapabilityError ? error.code : 'SCHEMA_ERROR');
    }
    advance(); reportError(null);
  }
  function acceptOperation(input: unknown): boolean {
    let changed: { package_id: string; key: string } | undefined;
    let invocationId: string;
    let failureCode = 'LOCAL_STORAGE_UNAVAILABLE';
    try {
      requireValue(plain(input));
      keys(input, ['clientProtocolVersion', 'invocation_id', 'connection_epoch', 'package_id',
        'activation_generation', 'method', 'params', 'deadline']);
      requireValue(input.clientProtocolVersion === 1 && nonEmpty(input.invocation_id));
      requireValue(declarations && input.connection_epoch === declarations.connectionEpoch, 'STALE_CONTEXT');
      const pkg = declarations.packages.find((item) => item.packageId === input.package_id);
      requireValue(pkg && input.activation_generation === pkg.activationGeneration, 'PACKAGE_INACTIVE');
      requireValue(typeof input.deadline === 'number' && Number.isFinite(input.deadline) && input.deadline > Date.now(), 'DEADLINE_EXCEEDED');
      if (input.method === 'page.reload') {
        requireValue(plain(input.params)); keys(input.params, []);
        requireValue(options.reloadPage, 'CAPABILITY_UNAVAILABLE');
        failureCode = 'PAGE_RELOAD_FAILED';
        options.reloadPage();
        reportError(null);
        return true;
      }
      requireValue(input.method === 'local_storage.set' || input.method === 'local_storage.delete', 'UNKNOWN_METHOD');
      requireValue(plain(input.params)); keys(input.params, input.method === 'local_storage.set' ? ['key', 'value'] : ['key']);
      requireValue(nonEmpty(input.params.key) && bytes(input.params.key) <= 128);
      const key = keyFor(pkg.packageId, input.params.key);
      const storage = options.storage();
      const previous = storage.getItem(key);
      let next: string | null;
      if (input.method === 'local_storage.set') {
        validateJSON(input.params.value);
        const encoded = JSON.stringify(input.params.value);
        requireValue(bytes(encoded) <= 64 * 1024, 'VALUE_TOO_LARGE');
        storage.setItem(key, encoded);
        next = encoded;
      } else {
        storage.removeItem(key);
        next = null;
      }
      invocationId = input.invocation_id;
      if (previous !== next) changed = { package_id: pkg.packageId, key: input.params.key };
    } catch (error) {
      reportError(error instanceof ClientCapabilityError ? error.code
        : error instanceof DOMException && error.name === 'QuotaExceededError' ? 'QUOTA_EXCEEDED' : failureCode);
      return false;
    }
    capture(invocationId);
    if (changed) for (const listener of storageListeners) listener(changed);
    reportError(null);
    return true;
  }
  function bindWebSocket<T extends { method: string }>(message: T): T & { request_context?: Record<string, unknown> } {
    requireValue(!awaitingDeclarations, 'CLIENT_DECLARATIONS_REQUIRED');
    requireValue(!invalidConfiguration, 'CONFIGURATION_ERROR');
    if (!declarations) return message;
    const current = capture();
    const requestContext: Record<string, unknown> = {};
    allBindings().forEach(({ binding }, index) => {
      if (binding.type !== 'websocket' || binding.target.location !== 'message' || current[index] === undefined) return;
      let parent = requestContext;
      const parts = binding.target.path.slice(1);
      parts.forEach((part, offset) => {
        if (offset === parts.length - 1) parent[part] = current[index];
        else {
          if (parent[part] === undefined) parent[part] = {};
          requireValue(plain(parent[part]), 'BINDING_CONFLICT');
          parent = parent[part] as Record<string, unknown>;
        }
      });
    });
    return Object.keys(requestContext).length > 0 ? { ...message, request_context: requestContext } : message;
  }
  function bindHTTP(input: string | URL | Request, headers?: HeadersInit): Headers {
    const result = new Headers(headers);
    if (isPluginResourceURL(input, options.gatewayUrl())) return result;
    requireValue(!awaitingDeclarations, 'CLIENT_DECLARATIONS_REQUIRED');
    requireValue(!invalidConfiguration, 'CONFIGURATION_ERROR');
    if (!declarations || !isGateway(input)) return result;
    const current = capture();
    allBindings().forEach(({ binding }, index) => {
      if (binding.type !== 'http' || binding.target.location !== 'header' || current[index] === undefined) return;
      requireValue(!result.has(binding.target.name), 'BINDING_CONFLICT');
      result.set(binding.target.name, current[index]!);
    });
    return result;
  }
  return {
    configure, acceptOperation, bindWebSocket, bindHTTP, isGateway, getGatewayUrl: options.gatewayUrl,
    refresh: () => { if (declarations) capture(); },
    getEpoch: () => epoch,
    isChanging: () => changing,
    getSignal: () => controller.signal,
    hasDeclarations: () => declarations !== null || invalidConfiguration || awaitingDeclarations,
    isUnavailable: () => invalidConfiguration || awaitingDeclarations,
    hasHTTPBindings: () => allBindings().some(({ binding }) => binding.type === 'http'),
    setInvocationWaiter: (next: (id: string) => Promise<void>) => { waiter = next; },
    subscribe: (listener: (change: ClientContextChange) => void) => { changes.add(listener); return () => { changes.delete(listener); }; },
    subscribeErrors: (listener: () => void) => { errors.add(listener); return () => { errors.delete(listener); }; },
    subscribeStorage: (listener: (change: { package_id: string; key: string }) => void) => {
      storageListeners.add(listener); return () => { storageListeners.delete(listener); };
    },
    getError: () => errorCode,
    clearError: () => reportError(null),
    acceptsFrame: (input: unknown) => {
      if (!plain(input) || !plain(input.target)) return false;
      const packageId = input.target.package_id;
      const pkg = declarations?.packages.find((item) => item.packageId === packageId);
      if (!Object.prototype.hasOwnProperty.call(input, 'activation_generation')) return !pkg;
      return !awaitingDeclarations && !invalidConfiguration && !!pkg
        && input.activation_generation === pkg.activationGeneration;
    },
    invalidate: () => { declarations = null; invalidConfiguration = false; awaitingDeclarations = false; signature = ''; advance(); },
    suspendConnection: () => {
      if ((!declarations && !invalidConfiguration) || awaitingDeclarations) return;
      declarations = null; signature = ''; awaitingDeclarations = true;
      controller.abort(); controller = new AbortController(); changing = true;
      const current = ++epoch;
      for (const listener of changes) listener({ epoch: current, phase: 'changing' });
    },
    storageChanged: (event: Pick<StorageEvent, 'key'>) => {
      if (!declarations) return;
      if (event.key === null) { capture(); return; }
      if (!event.key.startsWith(PREFIX)) return;
      try {
        const decoded: unknown = JSON.parse(event.key.slice(PREFIX.length));
        if (!Array.isArray(decoded) || decoded.length !== 3 || decoded[0] !== declarations.gatewayInstanceId
          || !declarations.packages.some((pkg) => pkg.packageId === decoded[1]) || typeof decoded[2] !== 'string') return;
        capture();
        for (const listener of storageListeners) listener({ package_id: decoded[1], key: decoded[2] });
      } catch { /* Other origin storage keys are not extension data. */ }
    },
  };
}

export type ClientCapabilityHost = ReturnType<typeof createClientCapabilityHost>;
export type { ClientDeclarationSnapshot, ClientOperation, RequestBinding };
