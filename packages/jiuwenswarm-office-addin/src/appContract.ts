/**
 * An app the pane runs in, and its tool contract shared with the server: the
 * ``tools.json`` and ``profile.json`` of the app's package directory under
 * ``jiuwenswarm/office/``, which the backend loads too (``@office``, see
 * vite.config.ts). The pane never edits a contract; it only reports which of
 * its tools it can run in the running host.
 */
import type { DocumentId } from './documentIdentity';
import { isRecord } from './gatewayClient';

/** Who makes the host application; WPS comes as a second platform. */
export type Platform = 'microsoft';
/** What the host edits, as the attach names it (``resource.kind``). */
export type Surface = 'presentation' | 'document' | 'workbook';

/** One host application the pane runs in: a platform's app for a surface. */
export interface OfficeApp {
  platform: Platform;
  surface: Surface;
  /** The host's name, as the model reads it. */
  host: string;
  /** The ``client_app`` the backend registers the app's conversations under. */
  clientApp: string;
  /** ``profile.json`` ``app_id``: the app the Gateway connection is for. */
  appId: string;
  contract: ToolContract;
  /** The tools whose answers carry their fields at the top level, as the backend reads them. */
  flatResults?: ReadonlySet<string>;
}

/** A node of an ``input_schema``: the part of JSON Schema the contract's tools use. */
interface Schema {
  type?: string;
  properties?: Record<string, Schema>;
  required?: string[];
  additionalProperties?: boolean | Schema;
  enum?: unknown[];
  const?: unknown;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  maxItems?: number;
  pattern?: string;
  items?: Schema;
  oneOf?: Schema[];
  anyOf?: Schema[];
}

/** The keywords ``checkInput`` checks, and the annotations it reads past; any other one throws. */
const KEYWORDS = new Set(['type', 'properties', 'required', 'additionalProperties', 'enum', 'const', 'minimum',
  'maximum', 'exclusiveMinimum', 'minLength', 'maxLength', 'minItems', 'maxItems', 'pattern', 'items', 'oneOf', 'anyOf',
  'description', 'default']);

/** What a host must offer to run a tool: every requirement set at its version, and a Worker if ``worker``. */
interface HostRequirements {
  sets: Record<string, string>;
  worker?: boolean;
}

export interface ContractTool {
  name: string;
  /** A ``Schema``; the JSON's inferred type is a union of every tool's fields, which no one interface matches. */
  input_schema: object;
  annotations: { mutating: boolean };
  host_requirements: HostRequirements;
}

/** What the running Office host offers the contract's ``host_requirements``. */
export interface OfficeHost {
  isSetSupported(name: string, version: string): boolean;
  worker: boolean;
}

/** An app's ``tools.json``. */
export class ToolContract {
  private readonly byName: ReadonlyMap<string, ContractTool>;

  constructor(private readonly tools: readonly ContractTool[]) {
    this.byName = new Map(tools.map(tool => [tool.name, tool]));
  }

  has(name: string): boolean {
    return this.byName.has(name);
  }

  isMutating(name: string): boolean {
    return this.byName.get(name)?.annotations.mutating === true;
  }

  /** What is wrong with ``input`` by the tool's ``input_schema``, and where; undefined when the schema allows it. */
  checkInput(name: string, input: unknown): string | undefined {
    const tool = this.byName.get(name);
    if (!tool) throw new Error(`${name} is not part of the contract`);
    return problem(tool.input_schema as Schema, input, '');
  }

  /** The tools that are in the contract, implemented by the pane's tool runtime, and supported by the host. */
  supported(host: OfficeHost, implemented: ReadonlySet<string>): string[] {
    return this.tools.filter(({ name, host_requirements: { sets, worker } }) => implemented.has(name)
      && Object.entries(sets).every(([set, version]) => host.isSetSupported(set, version))
      && (!worker || host.worker)).map(tool => tool.name);
  }
}

function problem(schema: Schema, value: unknown, path: string): string | undefined {
  const unchecked = Object.keys(schema).find(keyword => !KEYWORDS.has(keyword));
  if (unchecked) throw new Error(`input_schema keyword ${unchecked} is not one the pane checks`);
  const at = path || 'the input';
  if (schema.const !== undefined && value !== schema.const) return `${at} must be ${JSON.stringify(schema.const)}`;
  if (schema.enum && !schema.enum.includes(value)) {
    return `${at} must be one of ${schema.enum.map(item => JSON.stringify(item)).join(', ')}`;
  }
  if (schema.type && !isType(value, schema.type)) {
    return `${at} must be ${schema.type === 'null' ? 'null' : `${schema.type === 'array' || schema.type === 'object'
      || schema.type === 'integer' ? 'an' : 'a'} ${schema.type}`}`;
  }
  if (typeof value === 'number') {
    if (schema.minimum !== undefined && value < schema.minimum) return `${at} must be at least ${schema.minimum}`;
    if (schema.exclusiveMinimum !== undefined && value <= schema.exclusiveMinimum) {
      return `${at} must be greater than ${schema.exclusiveMinimum}`;
    }
    if (schema.maximum !== undefined && value > schema.maximum) return `${at} must be at most ${schema.maximum}`;
  }
  if (typeof value === 'string') {
    // JSON Schema counts characters, not UTF-16 units.
    const length = [...value].length;
    if (schema.minLength !== undefined && length < schema.minLength) {
      return `${at} must have at least ${schema.minLength} characters`;
    }
    if (schema.maxLength !== undefined && length > schema.maxLength) {
      return `${at} must have at most ${schema.maxLength} characters`;
    }
    if (schema.pattern !== undefined && !new RegExp(schema.pattern).test(value)) {
      return `${at} must match ${schema.pattern}`;
    }
  }
  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) {
      return `${at} must have at least ${schema.minItems} items`;
    }
    if (schema.maxItems !== undefined && value.length > schema.maxItems) {
      return `${at} must have at most ${schema.maxItems} items`;
    }
    for (const [index, item] of value.entries()) {
      const found = schema.items && problem(schema.items, item, `${path}[${index}]`);
      if (found) return found;
    }
  }
  if (isRecord(value)) {
    const field = (key: string) => (path ? `${path}.${key}` : key);
    const missing = schema.required?.find(key => !Object.hasOwn(value, key));
    if (missing) return `${field(missing)} is required`;
    const properties = schema.properties ?? {};
    for (const [key, item] of Object.entries(value)) {
      const rule = Object.hasOwn(properties, key) ? properties[key] : schema.additionalProperties;
      if (rule === false) return `${field(key)} is not a field here; the fields are ${Object.keys(properties).join(', ')}`;
      const found = typeof rule === 'object' ? problem(rule, item, field(key)) : undefined;
      if (found) return found;
    }
  }
  if (schema.anyOf) {
    const found = schema.anyOf.map(branch => problem(branch, value, path));
    if (found.every(item => item !== undefined)) return found.join('; or ');
  }
  return schema.oneOf && oneOfProblem(schema.oneOf, value, at, path);
}

/**
 * The contract's ``oneOf`` forms differ in a ``const`` field (a chart spec's ``type``), so at most one matches; the
 * problem is the one of the form the value names, or that it names none.
 */
function oneOfProblem(forms: Schema[], value: unknown, at: string, path: string): string | undefined {
  const found = forms.map(form => problem(form, value, path));
  if (found.includes(undefined)) return undefined;
  const constants = (form: Schema) => Object.entries(form.properties ?? {}).filter(([, rule]) => rule.const !== undefined);
  const named = forms.flatMap((form, index) => (constants(form)
    .every(([key, rule]) => isRecord(value) && value[key] === rule.const) ? [found[index]] : []));
  if (named.length === 1) return named[0];
  return `${at} must be one of its forms: ${forms.flatMap(form => constants(form)
    .map(([key, rule]) => `${key} ${JSON.stringify(rule.const)}`)).join(', ')}`;
}

function isType(value: unknown, type: string): boolean {
  switch (type) {
    case 'object': return isRecord(value);
    case 'array': return Array.isArray(value);
    case 'string': return typeof value === 'string';
    case 'boolean': return typeof value === 'boolean';
    case 'null': return value === null;
    case 'integer': return Number.isInteger(value);
    case 'number': return typeof value === 'number' && Number.isFinite(value);
    default: throw new Error(`input_schema type ${type} is not one the pane checks`);
  }
}

/** What a task pane sends with ``client.attach``, besides the conversation. */
export interface AttachDeclaration {
  resource: { key: DocumentId; display_name: string; kind: Surface; host_vendor: Platform };
  capabilities: { supported_tools: string[] };
}

export function attachDeclaration(app: OfficeApp, { documentId, displayName, host, implemented }: {
  documentId: DocumentId; displayName: string; host: OfficeHost; implemented: ReadonlySet<string>;
}): AttachDeclaration {
  return {
    resource: { key: documentId, display_name: displayName, kind: app.surface, host_vendor: app.platform },
    capabilities: { supported_tools: app.contract.supported(host, implemented) },
  };
}
