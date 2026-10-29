/**
 * 树校验与规范化：遍历 wire 节点，校验组件、props、children、action 与规模，
 * 补充目录声明的默认值并生成路径 key。返回新对象，不修改输入。
 */

import type { WebUICatalog, WebUIComponentRule, WebUIPropRule } from '../catalog/types.ts';
import type { WebUINormalizedAction, WebUINormalizedNode } from '../contracts/normalized.ts';
import { protocolOk, protocolReject, type WebUIProtocolResult } from '../contracts/results.ts';
import type { WebUILimits } from './limits.ts';
import { isRecord, jsonByteLength, readNonEmptyString, utf8ByteLength } from './utils.ts';

export interface NormalizedTreeResult {
  tree: WebUINormalizedNode;
  nodeCount: number;
}

interface WalkContext {
  catalog: WebUICatalog;
  limits: WebUILimits;
  nodes: number;
}

export function normalizeTree(
  root: unknown,
  catalog: WebUICatalog,
  limits: WebUILimits,
): WebUIProtocolResult<NormalizedTreeResult> {
  const context: WalkContext = { catalog, limits, nodes: 0 };
  const result = normalizeNode(root, '0', 1, context);
  if (!result.ok) {
    return result;
  }
  return protocolOk({ tree: result.value, nodeCount: context.nodes });
}

function normalizeNode(
  raw: unknown,
  key: string,
  depth: number,
  context: WalkContext,
): WebUIProtocolResult<WebUINormalizedNode> {
  const { limits } = context;
  if (depth > limits.maxDepth) {
    return protocolReject('SCHEMA_ERROR', `tree depth exceeds ${limits.maxDepth}`);
  }
  context.nodes += 1;
  if (context.nodes > limits.maxNodes) {
    return protocolReject('SCHEMA_ERROR', `node count exceeds ${limits.maxNodes}`);
  }
  if (!isRecord(raw)) {
    return protocolReject('SCHEMA_ERROR', 'node must be an object');
  }

  const component = readNonEmptyString(raw.component);
  if (!component) {
    return protocolReject('SCHEMA_ERROR', 'node.component is required');
  }
  const rule = context.catalog.getComponent(component);
  if (!rule) {
    return protocolReject('UNKNOWN_COMPONENT', `component not registered: ${component}`);
  }

  const props = normalizeProps(raw.props, rule, limits);
  if (!props.ok) {
    return props;
  }
  const action = normalizeAction(raw.action, rule, limits);
  if (!action.ok) {
    return action;
  }
  const children = normalizeChildren(raw.children, rule, key, depth, context);
  if (!children.ok) {
    return children;
  }

  return protocolOk({
    key,
    component,
    props: props.value,
    children: children.value,
    action: action.value,
  });
}

function normalizeProps(
  raw: unknown,
  rule: WebUIComponentRule,
  limits: WebUILimits,
): WebUIProtocolResult<Record<string, unknown>> {
  if (raw === undefined || raw === null) {
    return protocolOk({ ...(rule.defaults ?? {}) });
  }
  if (!isRecord(raw)) {
    return protocolReject('SCHEMA_ERROR', 'node.props must be an object');
  }

  const normalized: Record<string, unknown> = { ...(rule.defaults ?? {}) };
  for (const [name, value] of Object.entries(raw)) {
    if (value === undefined) {
      continue;
    }
    const propRule = rule.props[name];
    if (!propRule) {
      return protocolReject('SCHEMA_ERROR', `props not allowed for ${rule.name}: ${name}`);
    }
    const error = checkPropValue(propRule, value, limits);
    if (error) {
      return protocolReject('SCHEMA_ERROR', `invalid ${name}: ${error}`);
    }
    normalized[name] = value;
  }
  return protocolOk(normalized);
}

function checkPropValue(propRule: WebUIPropRule, value: unknown, limits: WebUILimits): string | null {
  switch (propRule.kind) {
    case 'token':
      if (typeof value !== 'string' || !propRule.values.includes(value)) {
        return `expected one of ${propRule.values.join('/')}`;
      }
      return null;
    case 'string':
      if (typeof value !== 'string') {
        return 'expected a string';
      }
      if (utf8ByteLength(value) > (propRule.maxBytes ?? limits.maxTextBytes)) {
        return 'string is too long';
      }
      return null;
    case 'number':
      if (typeof value !== 'number' || !Number.isFinite(value)) {
        return 'expected a finite number';
      }
      if (propRule.integer === true && !Number.isInteger(value)) {
        return 'expected an integer';
      }
      if (propRule.min !== undefined && value < propRule.min) {
        return `expected >= ${propRule.min}`;
      }
      if (propRule.max !== undefined && value > propRule.max) {
        return `expected <= ${propRule.max}`;
      }
      return null;
    case 'boolean':
      return typeof value === 'boolean' ? null : 'expected a boolean';
    case 'string-list':
      if (!Array.isArray(value)) {
        return 'expected a string array';
      }
      if (value.length > (propRule.maxItems ?? limits.maxListItems)) {
        return 'array has too many items';
      }
      return value.every((item) => typeof item === 'string') ? null : 'expected a string array';
    case 'record-list':
      if (!Array.isArray(value)) {
        return 'expected an array of objects';
      }
      if (value.length > (propRule.maxItems ?? limits.maxListItems)) {
        return 'array has too many items';
      }
      for (const item of value) {
        if (!isRecord(item) || !Object.values(item).every(isScalar)) {
          return 'expected an array of flat objects';
        }
      }
      return null;
    default:
      return 'unsupported prop rule';
  }
}

function normalizeChildren(
  raw: unknown,
  rule: WebUIComponentRule,
  parentKey: string,
  depth: number,
  context: WalkContext,
): WebUIProtocolResult<readonly WebUINormalizedNode[]> {
  if (raw === undefined || raw === null) {
    return protocolOk([]);
  }
  if (!Array.isArray(raw)) {
    return protocolReject('SCHEMA_ERROR', 'node.children must be an array');
  }
  if (raw.length === 0) {
    return protocolOk([]);
  }
  if (!rule.allowChildren) {
    return protocolReject('SCHEMA_ERROR', `component ${rule.name} does not accept children`);
  }

  const children: WebUINormalizedNode[] = [];
  for (let index = 0; index < raw.length; index += 1) {
    const child = normalizeNode(raw[index], `${parentKey}.${index}`, depth + 1, context);
    if (!child.ok) {
      return child;
    }
    children.push(child.value);
  }
  return protocolOk(children);
}

function normalizeAction(
  raw: unknown,
  rule: WebUIComponentRule,
  limits: WebUILimits,
): WebUIProtocolResult<WebUINormalizedAction | null> {
  if (raw === undefined || raw === null) {
    return protocolOk(null);
  }
  if (!isRecord(raw)) {
    return protocolReject('SCHEMA_ERROR', 'node.action must be an object');
  }
  if (!rule.allowAction) {
    return protocolReject('SCHEMA_ERROR', `component ${rule.name} does not accept an action`);
  }

  const id = readNonEmptyString(raw.id);
  if (!id) {
    return protocolReject('SCHEMA_ERROR', 'action.id is required');
  }
  if (utf8ByteLength(id) > limits.maxTargetStringBytes) {
    return protocolReject('SCHEMA_ERROR', 'action.id is too long');
  }

  let args: Record<string, unknown> = {};
  if (raw.args !== undefined && raw.args !== null) {
    if (!isRecord(raw.args)) {
      return protocolReject('SCHEMA_ERROR', 'action.args must be an object');
    }
    // 与后端 _validate_action 保持一致：允许标量或数组，拒绝其它结构。
    for (const [name, value] of Object.entries(raw.args)) {
      if (!isScalar(value) && !Array.isArray(value)) {
        return protocolReject('SCHEMA_ERROR', `action.args field not supported: ${name}`);
      }
    }
    if (jsonByteLength(raw.args) > limits.maxActionArgsBytes) {
      return protocolReject('SCHEMA_ERROR', 'action.args is too large');
    }
    args = { ...raw.args };
  }

  return protocolOk({ id, args });
}

function isScalar(value: unknown): boolean {
  return value === null || typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean';
}
