/**
 * 协议处理链：消息检查 → 树校验/规范化 → 结构化结果。
 * 全部是纯函数：不读业务状态、不操作 DOM、不写实例状态。
 */

import type { WebUICatalog } from '../catalog/types.ts';
import { WEBUI_COMPONENT_SCHEMA_VERSION } from '../contracts/wire.ts';
import type {
  WebUINormalizedClearFrame,
  WebUINormalizedErrorFrame,
  WebUINormalizedRenderFrame,
  WebUINormalizedTarget,
} from '../contracts/normalized.ts';
import {
  protocolOk,
  protocolReject,
  protocolUnsupported,
  type WebUIProtocolResult,
} from '../contracts/results.ts';
import { DEFAULT_WEBUI_LIMITS, type WebUILimits } from './limits.ts';
import { normalizeTree } from './normalizeTree.ts';
import { freezeJSON, isJSONData, isRecord, jsonByteLength, readNonEmptyString, utf8ByteLength } from './utils.ts';

/** 只能作为整棵渲染树根、不能嵌套的组件。 */
const ROOT_COMPONENTS: readonly string[] = ['Panel', 'Iframe'];

export interface WebUIProtocol {
  readonly catalog: WebUICatalog;
  readonly limits: WebUILimits;
  processRenderFrame(input: unknown): WebUIProtocolResult<WebUINormalizedRenderFrame>;
  processClearFrame(input: unknown): WebUIProtocolResult<WebUINormalizedClearFrame>;
  processErrorFrame(input: unknown): WebUIProtocolResult<WebUINormalizedErrorFrame>;
}

export function createProtocolPipeline(
  catalog: WebUICatalog,
  limits: WebUILimits = DEFAULT_WEBUI_LIMITS,
): WebUIProtocol {
  return {
    catalog,
    limits,
    processRenderFrame: (input) => processRenderFrame(input, catalog, limits),
    processClearFrame: (input) => processClearFrame(input, catalog, limits),
    processErrorFrame: (input) => processErrorFrame(input, catalog, limits),
  };
}

export function processRenderFrame(
  input: unknown,
  catalog: WebUICatalog,
  limits: WebUILimits = DEFAULT_WEBUI_LIMITS,
): WebUIProtocolResult<WebUINormalizedRenderFrame> {
  if (!isRecord(input)) {
    return protocolReject('SCHEMA_ERROR', 'render frame must be an object');
  }
  const sizeFailure = checkFrameSize(input, limits);
  if (sizeFailure) {
    return sizeFailure;
  }

  const target = readTarget(input.target, catalog, limits);
  if (!target.ok) {
    return target;
  }
  const revision = readRevision(input.revision, 'revision');
  if (!revision.ok) {
    return revision;
  }

  if (typeof input.schema_version !== 'number') {
    return protocolReject('SCHEMA_ERROR', 'schema_version is required');
  }
  if (input.schema_version !== WEBUI_COMPONENT_SCHEMA_VERSION) {
    return protocolUnsupported(
      'UNSUPPORTED_SCHEMA_VERSION',
      `unsupported schema_version: ${input.schema_version}`,
    );
  }

  if (input.mode !== 'replace') {
    return protocolUnsupported('UNSUPPORTED_MODE', `unsupported mode: ${String(input.mode)}`);
  }
  if (!isJSONData(input)) return protocolReject('SCHEMA_ERROR', 'frame must contain only plain JSON data');

  if (input.root === null || input.root === undefined) {
    return protocolReject('SCHEMA_ERROR', 'render tree is required');
  }
  const root: unknown = JSON.parse(JSON.stringify(input.root));
  const tree = normalizeTree(root, catalog, limits);
  if (!tree.ok) {
    return tree;
  }
  const rootComponent = tree.value.tree.component;
  const slotRootComponents: readonly string[] = catalog.getSlot(target.value.slotId)?.rootComponents ?? [];
  if (target.value.slotId === 'conversation.panel.right') {
    if (!slotRootComponents.includes(rootComponent)) {
      return protocolReject('SCHEMA_ERROR', `right panel requires one of: ${slotRootComponents.join(', ')}`);
    }
  } else if (ROOT_COMPONENTS.includes(rootComponent) && !slotRootComponents.includes(rootComponent)) {
    return protocolReject(
      'SCHEMA_ERROR',
      `${rootComponent} is only allowed in slots that accept it as the render root`,
    );
  }

  return protocolOk({
    kind: 'render',
    target: target.value,
    revision: revision.value,
    schemaVersion: input.schema_version,
    tree: freezeJSON(tree.value.tree),
    nodeCount: tree.value.nodeCount,
  });
}

export function processClearFrame(
  input: unknown,
  catalog: WebUICatalog,
  limits: WebUILimits = DEFAULT_WEBUI_LIMITS,
): WebUIProtocolResult<WebUINormalizedClearFrame> {
  if (!isRecord(input)) {
    return protocolReject('SCHEMA_ERROR', 'clear frame must be an object');
  }
  const sizeFailure = checkFrameSize(input, limits);
  if (sizeFailure) {
    return sizeFailure;
  }
  const target = readTarget(input.target, catalog, limits);
  if (!target.ok) {
    return target;
  }
  const revision = readRevision(input.revision, 'revision');
  if (!revision.ok) {
    return revision;
  }
  if (input.reason !== undefined && typeof input.reason !== 'string') {
    return protocolReject('SCHEMA_ERROR', 'reason must be a string');
  }
  return protocolOk({
    kind: 'clear',
    target: target.value,
    revision: revision.value,
    reason: typeof input.reason === 'string' ? input.reason : '',
  });
}

export function processErrorFrame(
  input: unknown,
  catalog: WebUICatalog,
  limits: WebUILimits = DEFAULT_WEBUI_LIMITS,
): WebUIProtocolResult<WebUINormalizedErrorFrame> {
  if (!isRecord(input)) {
    return protocolReject('SCHEMA_ERROR', 'error frame must be an object');
  }
  const sizeFailure = checkFrameSize(input, limits);
  if (sizeFailure) {
    return sizeFailure;
  }
  const target = readTarget(input.target, catalog, limits);
  if (!target.ok) {
    return target;
  }

  let revision = 0;
  if (input.revision !== undefined) {
    const parsedRevision = readRevision(input.revision, 'revision');
    if (!parsedRevision.ok) {
      return parsedRevision;
    }
    revision = parsedRevision.value;
  }

  const code = readNonEmptyString(input.code);
  if (!code) {
    return protocolReject('SCHEMA_ERROR', 'error code is required');
  }
  if (input.message !== undefined && typeof input.message !== 'string') {
    return protocolReject('SCHEMA_ERROR', 'error message must be a string');
  }
  return protocolOk({
    kind: 'error',
    target: target.value,
    revision,
    code,
    message: typeof input.message === 'string' ? input.message : '',
  });
}

function checkFrameSize(
  input: Record<string, unknown>,
  limits: WebUILimits,
): WebUIProtocolResult<never> | null {
  if (jsonByteLength(input) > limits.maxFrameBytes) {
    return protocolReject('FRAME_TOO_LARGE', `frame exceeds ${limits.maxFrameBytes} bytes`);
  }
  return null;
}

function readTarget(
  raw: unknown,
  catalog: WebUICatalog,
  limits: WebUILimits,
): WebUIProtocolResult<WebUINormalizedTarget> {
  if (!isRecord(raw)) {
    return protocolReject('SCHEMA_ERROR', 'target is required');
  }
  if (typeof raw.slot_id !== 'string') {
    return protocolReject('SCHEMA_ERROR', 'target.slot_id is required');
  }
  if (!catalog.getSlot(raw.slot_id)) {
    return protocolReject('SCHEMA_ERROR', `slot is not allowed: ${raw.slot_id}`);
  }
  if (raw.application_id !== undefined) {
    return protocolReject('SCHEMA_ERROR', 'application targets are not supported');
  }
  if (typeof raw.session_id !== 'string' || raw.session_id.trim().length === 0) {
    return protocolReject('SCHEMA_ERROR', 'target.session_id is required');
  }
  if (typeof raw.package_id !== 'string' || typeof raw.instance_key !== 'string') {
    return protocolReject('SCHEMA_ERROR', 'target.package_id and target.instance_key are required');
  }
  for (const [name, value] of [
    ['package_id', raw.package_id],
    ['instance_key', raw.instance_key],
  ] as const) {
    if (utf8ByteLength(value) > limits.maxTargetStringBytes) {
      return protocolReject('SCHEMA_ERROR', `target.${name} is too long`);
    }
  }
  return protocolOk({
    sessionId: raw.session_id,
    slotId: raw.slot_id,
    packageId: raw.package_id,
    instanceKey: raw.instance_key,
  });
}

function readRevision(raw: unknown, field: string): WebUIProtocolResult<number> {
  if (typeof raw !== 'number' || !Number.isInteger(raw) || raw < 0) {
    return protocolReject('SCHEMA_ERROR', `${field} must be a non-negative integer`);
  }
  return protocolOk(raw);
}
