/**
 * 树校验与规范化：遍历 wire 节点，校验组件、props、children、events 与规模，
 * 补充目录声明的默认值并生成路径 key。返回新对象，不修改输入。
 */

import type { WebUICatalog, WebUIComponentRule, WebUIPropRule } from '../catalog/types.ts';
import type { WebUINormalizedAction, WebUINormalizedNode } from '../contracts/normalized.ts';
import { protocolOk, protocolReject, type WebUIProtocolResult } from '../contracts/results.ts';
import type { WebUILimits } from './limits.ts';
import { checkPropValue } from './checkPropValue.ts';
import { isRecord, jsonByteLength, readNonEmptyString, utf8ByteLength } from './utils.ts';

const actionArgRule: WebUIPropRule = {
  kind: 'lazy',
  get: () => ({
    kind: 'union', choices: [
      { kind: 'null' }, { kind: 'boolean' }, { kind: 'number' }, { kind: 'string' },
      { kind: 'array', item: actionArgRule }, { kind: 'record', value: actionArgRule },
    ],
  }),
};

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
  if (Object.keys(raw).some((name) => !['component', 'props', 'children', 'events'].includes(name))) {
    return protocolReject('SCHEMA_ERROR', 'unknown node field; use events to declare actions');
  }

  const component = readNonEmptyString(raw.component);
  if (!component) {
    return protocolReject('SCHEMA_ERROR', 'node.component is required');
  }
  const rule = context.catalog.getComponent(component);
  if (!rule) {
    return protocolReject('UNKNOWN_COMPONENT', `component not registered: ${component}`);
  }
  if ((component === 'Panel' || component === 'Iframe') && depth !== 1) {
    return protocolReject('SCHEMA_ERROR', `${component} is only allowed at the tree root`);
  }

  const props = normalizeProps(raw.props, rule, limits);
  if (!props.ok) {
    return props;
  }
  const children = normalizeChildren(raw.children, rule, key, depth, context);
  if (!children.ok) {
    return children;
  }
  if (component === 'Panel' && children.value.length === 0) {
    return protocolReject('SCHEMA_ERROR', 'Panel children are required');
  }
  if (rule.childComponents && children.value.some((child) => !rule.childComponents?.includes(child.component))) {
    return protocolReject('SCHEMA_ERROR', 'unsupported child component');
  }
  if (rule.name === 'Divider') {
    if (raw.children !== undefined && Object.prototype.hasOwnProperty.call(props.value, 'children')) {
      return protocolReject('SCHEMA_ERROR', 'Divider title sources are mutually exclusive');
    }
    if (props.value.orientation === 'vertical' && children.value.length > 0) {
      return protocolReject('SCHEMA_ERROR', 'vertical Divider cannot have a title');
    }
  }

  const events: Record<string, WebUINormalizedAction> = {};
  const contentGroups: Record<string, readonly WebUINormalizedNode[]> = {};
  if (raw.events !== undefined && (!isRecord(raw.events) || Object.keys(raw.events).length === 0)) {
    return protocolReject('SCHEMA_ERROR', 'events must be a non-empty object');
  }
  for (const [event, declaration] of Object.entries(raw.events ?? {})) {
    if (!Object.prototype.hasOwnProperty.call(rule.events ?? {}, event)) return protocolReject('SCHEMA_ERROR', `event not allowed: ${event}`);
    if (!isRecord(declaration) || Object.keys(declaration).some((name) => !['id', 'args'].includes(name))) {
      return protocolReject('SCHEMA_ERROR', 'invalid event action');
    }
    if (declaration.args !== undefined && !isRecord(declaration.args)) return protocolReject('SCHEMA_ERROR', 'args must be an object');
    const binding = normalizeEventAction(declaration, limits);
    if (!binding.ok) return binding;
    events[event] = binding.value;
  }
  if (rule.content) {
    const { prop, children: field } = rule.content;
    const items = props.value[prop] as Record<string, unknown>[];
    const metadata: Record<string, unknown>[] = [];
    for (let index = 0; index < items.length; index += 1) {
      const { [field]: nodes = [], ...item } = items[index];
      const group: WebUINormalizedNode[] = [];
      for (const [childIndex, child] of (nodes as unknown[]).entries()) {
        const result = normalizeNode(child, `${key}.${prop}.${index}.${field}.${childIndex}`, depth + 1, context);
        if (!result.ok) return result;
        group.push(result.value);
      }
      contentGroups[String(index)] = group;
      metadata.push(item);
    }
    props.value[prop] = metadata;
  }

  return protocolOk({
    key,
    component,
    props: props.value,
    children: children.value,
    events,
    contentGroups,
  });
}

function normalizeProps(
  raw: unknown,
  rule: WebUIComponentRule,
  limits: WebUILimits,
): WebUIProtocolResult<Record<string, unknown>> {
  if (raw === undefined) raw = {};
  if (!isRecord(raw)) {
    return protocolReject('SCHEMA_ERROR', 'node.props must be an object');
  }

  const normalized: Record<string, unknown> = { ...(rule.defaults ?? {}) };
  for (const [name, value] of Object.entries(raw)) {
    if (value === undefined) {
      continue;
    }
    const propRule = Object.prototype.hasOwnProperty.call(rule.props, name) ? rule.props[name] : undefined;
    if (!propRule) {
      return protocolReject('SCHEMA_ERROR', `props not allowed for ${rule.name}: ${name}`);
    }
    const error = checkPropValue(propRule, value, limits);
    if (error) {
      return protocolReject('SCHEMA_ERROR', `invalid ${name}: ${error}`);
    }
    normalized[name] = value;
  }
  for (const name of rule.required ?? []) {
    if (!Object.prototype.hasOwnProperty.call(normalized, name)) return protocolReject('SCHEMA_ERROR', `missing required prop: ${name}`);
  }
  const error = rule.validate?.(normalized);
  if (error) return protocolReject('SCHEMA_ERROR', error);
  return protocolOk(rule.prepare ? rule.prepare(normalized) : normalized);
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
  if (!rule.allowChildren) {
    return protocolReject('SCHEMA_ERROR', `component ${rule.name} does not accept children`);
  }
  if (raw.length > context.limits.maxListItems) {
    return protocolReject('SCHEMA_ERROR', 'too many children');
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

function normalizeEventAction(
  raw: unknown,
  limits: WebUILimits,
): WebUIProtocolResult<WebUINormalizedAction> {
  if (!isRecord(raw)) {
    return protocolReject('SCHEMA_ERROR', 'event action must be an object');
  }

  const id = readNonEmptyString(raw.id);
  if (!id || !id.trim()) {
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
    const error = checkPropValue({ kind: 'record', value: actionArgRule }, raw.args, limits);
    if (error) return protocolReject('SCHEMA_ERROR', `invalid action.args: ${error}`);
    if (jsonByteLength(raw.args) > limits.maxActionArgsBytes) {
      return protocolReject('SCHEMA_ERROR', 'action.args is too large');
    }
    args = { ...raw.args };
  }

  return protocolOk({ id, args });
}
