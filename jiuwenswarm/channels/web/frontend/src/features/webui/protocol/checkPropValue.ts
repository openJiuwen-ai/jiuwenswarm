import type { WebUIPropRule } from '../catalog/types.ts';
import type { WebUILimits } from './limits.ts';
import { isRecord, utf8ByteLength } from './utils.ts';

/** Validate data independently of rendering nodes and action declarations. */
export function checkPropValue(rule: WebUIPropRule, value: unknown, limits: WebUILimits, depth = 0): string | null {
  // Each recursive option level contains an array, an object and its scalar fields.
  if (depth > limits.maxDepth * 3) return 'data is too deep';
  switch (rule.kind) {
    case 'token':
      return typeof value === 'string' && rule.values.includes(value) ? null : `expected one of ${rule.values.join('/')}`;
    case 'string':
      return typeof value !== 'string' ? 'expected a string'
        : utf8ByteLength(value) > (rule.maxBytes ?? limits.maxTextBytes) ? 'string is too long' : null;
    case 'number':
      if (typeof value !== 'number' || !Number.isFinite(value)) return 'expected a finite number';
      if (rule.integer && !Number.isInteger(value)) return 'expected an integer';
      if (rule.min !== undefined && value < rule.min) return `expected >= ${rule.min}`;
      if (rule.max !== undefined && value > rule.max) return `expected <= ${rule.max}`;
      return null;
    case 'boolean': return typeof value === 'boolean' ? null : 'expected a boolean';
    case 'null': return value === null ? null : 'expected null';
    case 'union':
      return rule.choices.some((choice) => checkPropValue(choice, value, limits, depth) === null)
        ? null : 'value does not match any allowed type';
    case 'lazy': return checkPropValue(rule.get(), value, limits, depth);
    case 'object': {
      if (!isRecord(value)) return 'expected an object';
      if (Object.keys(value).length > limits.maxListItems) return 'object has too many entries';
      for (const name of rule.required ?? []) {
        if (!Object.prototype.hasOwnProperty.call(value, name)) return `missing required field: ${name}`;
      }
      for (const [name, item] of Object.entries(value)) {
        if (!Object.prototype.hasOwnProperty.call(rule.fields, name)) return `unknown field: ${name}`;
        const error = checkPropValue(rule.fields[name], item, limits, depth + 1);
        if (error) return `${name}: ${error}`;
      }
      return null;
    }
    case 'record': {
      if (!isRecord(value)) return 'expected an object';
      if (Object.keys(value).length > (rule.maxItems ?? limits.maxListItems)) return 'object has too many entries';
      for (const [key, item] of Object.entries(value)) {
        if (utf8ByteLength(key) > limits.maxTextBytes) return 'key is too long';
        const error = checkPropValue(rule.value, item, limits, depth + 1);
        if (error) return error;
      }
      return null;
    }
    case 'array':
    case 'string-list':
    case 'record-list':
    case 'nodes': {
      if (!Array.isArray(value)) return 'expected an array';
      const maxItems = 'maxItems' in rule ? rule.maxItems : undefined;
      if (value.length > (maxItems ?? limits.maxListItems)) return 'array has too many items';
      for (const item of value) {
        if (rule.kind === 'nodes') continue; // The common node walker validates the complete subtree.
        if (rule.kind === 'record-list') {
          if (!isRecord(item) || !Object.values(item).every(isScalar)) return 'expected an array of flat objects';
        } else {
          const error = checkPropValue(rule.kind === 'array' ? rule.item : { kind: 'string' }, item, limits, depth + 1);
          if (error) return error;
        }
      }
      return null;
    }
  }
}

export function isScalar(value: unknown): boolean {
  return value === null || typeof value === 'string' || typeof value === 'boolean'
    || (typeof value === 'number' && Number.isFinite(value));
}
