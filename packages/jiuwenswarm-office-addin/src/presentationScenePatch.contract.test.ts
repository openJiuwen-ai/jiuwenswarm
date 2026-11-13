/**
 * presentation_apply_patch's input_schema in tools.json against parseScenePatch: boundary values of every property
 * the schema offers go in, and what the parser takes must be what the schema allows. A new field, bound or enum
 * value on either side without the other fails here.
 */
import { expect, test } from 'vitest';

import tools from '@contract/tools.json';

import { parseScenePatch, PatchRefused } from './presentationScenePatch';

type S = Record<string, any>;
const tool = (tools as S[]).find(t => t.name === 'presentation_apply_patch')!;
const root = tool.input_schema as S;
const slideSchema = root.properties.slides.items as S;
const opSchema = slideSchema.properties.operations.items as S;

const ASSET = { key: 'img', handle: 'h1', purpose: 'input_image', mime_type: 'image/png', size: 1, sha256: 'a'.repeat(64) };
const DESIGN = { colors: { accent: '#18B7A0', text: '#0B1220', muted: '#526174', line: '#DDDDDD' } };
const G = { left: 10, top: 10, width: 100, height: 40 };
const BASES: Record<string, S> = {
  add_text: { op: 'add_text', text: 'x', ...G },
  add_shape: { op: 'add_shape', shape_type: 'Rectangle', text: 'x', ...G },
  add_image: { op: 'add_image', asset_key: 'img', ...G },
  add_table: { op: 'add_table', values: [['a', 'b'], ['1', '2']], left: 10, top: 10, width: 200, height: 60 },
  update_shape: { op: 'update_shape', shape_id: '2' },
  update_table: { op: 'update_table', shape_id: '2', row_height: 30 },
  delete_shape: { op: 'delete_shape', shape_id: '2' },
  // A free line, and one between two anchors: each takes fields the other refuses.
  add_connector: { op: 'add_connector', left: 10, top: 10, width: 100, height: 0 },
  'add_connector:anchored': { op: 'add_connector', start_anchor: { shape_name: 'a', side: 'right' },
    end_anchor: { shape_id: '2', side: 'left' } },
};
/** Which ops take each operation property, from the property descriptions ("add_table only", "Required for ..."). */
const USES: Record<string, string[]> = {
  op: ['add_text'], // each op's own base is accepted below; here only a value outside the enum
  name: ['add_text', 'add_shape', 'add_image', 'add_table', 'update_shape', 'add_connector'],
  text: ['add_text', 'add_shape', 'update_shape'],
  role: ['add_text', 'add_shape', 'update_shape'],
  text_style: ['add_text', 'add_shape', 'update_shape'],
  shape_type: ['add_shape'],
  shape_id: ['update_shape', 'update_table', 'delete_shape'],
  asset_key: ['add_image'], alt_text: ['add_image'],
  values: ['add_table'], header_row: ['add_table'], header_fill: ['add_table'], header_color: ['add_table'],
  font_size: ['add_table'], font_name: ['add_table'], color: ['add_table'], table_style: ['add_table'],
  rule_color: ['add_table'], rule_weight: ['add_table'], first_column_bold: ['add_table'],
  highlight_rows: ['add_table'], highlight_columns: ['add_table'], highlight_fill: ['add_table'],
  rotation: ['add_text', 'add_shape', 'add_image', 'update_shape'],
  z_order: ['update_shape'],
  fill: ['add_text', 'add_shape', 'update_shape'],
  line: ['add_text', 'add_shape', 'update_shape', 'add_connector'],
  left: ['add_text', 'add_shape', 'add_image', 'add_table', 'update_shape', 'add_connector'],
  top: ['add_text', 'add_shape', 'add_image', 'add_table', 'update_shape', 'add_connector'],
  width: ['add_text', 'add_shape', 'add_image', 'add_table', 'update_shape', 'add_connector'],
  height: ['add_text', 'add_shape', 'add_image', 'add_table', 'update_shape', 'add_connector'],
  column_widths: ['add_table', 'update_table'], row_height: ['add_table', 'update_table'],
  cell_styles: ['update_table'], row_rules: ['update_table'],
  connector_type: ['add_connector', 'add_connector:anchored'], end_arrow: ['add_connector'],
  start_anchor: ['add_connector:anchored'], end_anchor: ['add_connector:anchored'],
  waypoints: ['add_connector:anchored'],
};
/** Constraints the schema states only in prose; the probe expects these instead of the raw bounds. */
const PROSE: Record<string, { minimum?: number; skipLength?: boolean; length?: number }> = {
  // "At least 0.1 for shapes": the raw bounds are add_connector's, whose geometry may be 0 or negative.
  'width': { minimum: 0.1 },
  'height': { minimum: 0.1 },
  'add_connector:width': {},
  'add_connector:height': {},
  'add_table:column_widths': { skipLength: true, length: 2 }, // "one width in points per column"
  'input:assets': { skipLength: true, length: 1 }, // keys must differ; the schema cannot say so
};

function sample(schema: S, key = ''): any {
  if (key === 'cell_styles' && schema.type === 'object') return { first_row: 1, first_column: 1, bold: true };
  if (key === 'assets' && schema.type === 'object') return { key: 'img', handle: 'h1', purpose: 'input_image', mime_type: 'image/png', size: 1, sha256: 'a'.repeat(64) };
  if (schema.enum) return schema.enum[0];
  if (/color|fill|background/.test(key) && schema.type === 'string') return '#112233';
  if (key === 'sha256') return 'a'.repeat(64);
  if (key === 'key') return 'img';
  switch (schema.type) {
    case 'string': return 'x';
    case 'boolean': return true;
    case 'integer': case 'number': {
      if (schema.minimum !== undefined) return schema.minimum;
      if (schema.exclusiveMinimum !== undefined) return schema.exclusiveMinimum + 1;
      return 1;
    }
    case 'array': return Array.from({ length: Math.max(1, schema.minItems ?? 1) }, () => sample(schema.items, key));
    case 'object': {
      const out: S = {};
      for (const name of schema.required ?? []) out[name] = sample(schema.properties[name], name);
      if (schema.oneOf) Object.assign(out, { [schema.oneOf[0].required[0]]: 'x' });
      if (schema.minProperties && typeof schema.additionalProperties === 'object') out.accent = '#112233';
      if (key === 'design_system') out.colors = { accent: '#112233' };
      return out;
    }
  }
  return 'x';
}

interface Probe { where: string; value: any; expect: boolean; build: (value: any) => S }
const probes: Probe[] = [];

/** Boundary probes for one schema node, placed into a document by ``put``. */
function walk(schema: S, where: string, put: (value: any) => S, key: string, context: string) {
  const add = (value: any, expect: boolean, label = JSON.stringify(value)) =>
    probes.push({ where: `${where} = ${label}`, value, expect, build: put });
  const prose = PROSE[`${context}:${key}`] ?? PROSE[key] ?? {};
  if (schema.enum) {
    if (key !== 'op') for (const value of schema.enum) add(value, true);
    add('NotInEnum', false);
    return;
  }
  switch (schema.type) {
    case 'string':
      add(123, false);
      if (!schema.pattern && !/color|fill|background/.test(key) && key !== 'key' && key !== 'sha256') {
        add('', (schema.minLength ?? 0) === 0, '""');
      }
      if (schema.minLength) add('', false, '""');
      if (schema.pattern) add('!!not-matching!!', false);
      return;
    case 'boolean':
      add(true, true); add(false, true); add('true', false, '"true"');
      return;
    case 'integer': case 'number': {
      const step = schema.type === 'integer' ? 1 : 0.001;
      const minimum = prose.minimum ?? schema.minimum;
      if (schema.exclusiveMinimum !== undefined && prose.minimum === undefined) {
        add(schema.exclusiveMinimum + step, true); add(schema.exclusiveMinimum, false);
      } else if (minimum !== undefined) {
        add(minimum, true); add(minimum - step, false);
      }
      if (schema.maximum !== undefined) { add(schema.maximum, true); add(schema.maximum + step, false); }
      if (schema.type === 'integer') add((schema.minimum ?? 1) + 0.5, false);
      add('1', false, '"1"');
      return;
    }
    case 'array': {
      const item = sample(schema.items, key);
      if (!prose.skipLength) {
        if (schema.minItems !== undefined) {
          add(Array(schema.minItems).fill(item), true, `[${schema.minItems} items]`);
          if (schema.minItems > 0) add(Array(schema.minItems - 1).fill(item), false, `[${schema.minItems - 1} items]`);
        }
        if (schema.maxItems !== undefined) {
          add(Array(schema.maxItems).fill(item), true, `[${schema.maxItems} items]`);
          add(Array(schema.maxItems + 1).fill(item), false, `[${schema.maxItems + 1} items]`);
        }
      }
      const length = prose.length ?? Math.max(1, schema.minItems ?? 1);
      walk(schema.items, `${where}[*]`, value => put(Array(length).fill(value)), key, context);
      return;
    }
    case 'object': {
      const base = sample(schema, key);
      for (const [name, child] of Object.entries(schema.properties ?? {}) as [string, S][]) {
        walk(child, `${where}.${name}`, value => put({ ...base, [name]: value }), name, context);
      }
      for (const name of schema.required ?? []) {
        const { [name]: _gone, ...rest } = base;
        add(rest, false, `{without ${name}}`);
      }
      if (schema.additionalProperties === false) add({ ...base, zz_unknown: 1 }, false, '{+zz_unknown}');
      if (schema.additionalProperties && typeof schema.additionalProperties === 'object') {
        walk(schema.additionalProperties, `${where}.<any key>`, value => put({ ...base, zz: value }), 'color', context);
        const named = !schema.propertyNames?.pattern || new RegExp(schema.propertyNames.pattern).test('brandBlue');
        add({ ...base, brandBlue: '#112233' }, named, '{brandBlue: "#112233"}');
      }
      if (schema.minProperties) add({}, false, '{}');
      return;
    }
  }
}

const doc = (slide: S, extra: S = {}) => ({ base_structure_fingerprint: 'ppt-1', design_system: DESIGN, assets: [ASSET],
  slides: [slide], ...extra });
const newSlide = (operations: S[]) => ({ slide_ref: 'S1', operations });
const onSlide = (operations: S[]) => ({ slide_ref: 'S1', slide_id: '256#', operations });

// Operation properties, per op that takes them.
for (const [name, child] of Object.entries(opSchema.properties) as [string, S][]) {
  for (const op of USES[name] ?? []) {
    const slide = op.startsWith('add_') ? newSlide : onSlide;
    walk(child, `${op}.${name}`, value => doc(slide([{ ...BASES[op], [name]: value }])), name, op);
  }
}
// Slide-level properties.
const SLIDE_BASES: Record<string, S> = {
  slide_ref: newSlide([BASES.add_text]), slide_id: onSlide([BASES.update_shape]), layout_id: newSlide([BASES.add_text]),
  slide_master_id: { ...newSlide([BASES.add_text]), layout_id: 'l1' }, position: newSlide([BASES.add_text]),
  background: newSlide([BASES.add_text]), clear_existing_shapes: newSlide([BASES.add_text]),
  delete_slide: { slide_ref: 'S1', slide_id: '256#' }, move_to: { slide_ref: 'S1', slide_id: '256#' },
  operations: newSlide([]),
};
for (const [name, child] of Object.entries(slideSchema.properties) as [string, S][]) {
  if (name === 'operations') {
    for (const [label, slide] of [['new slide', { slide_ref: 'S1' }], ['existing slide', { slide_ref: 'S1', slide_id: '256#' }],
      ['existing slide + background', { slide_ref: 'S1', slide_id: '256#', background: '#FFFFFF' }],
      ['new slide + layout_id', { slide_ref: 'S1', layout_id: 'l1' }]] as [string, S][]) {
      probes.push({ where: `slide(${label}).operations = []`, value: [], expect: true, build: v => doc({ ...slide, operations: v }) });
      probes.push({ where: `slide(${label}).operations omitted`, value: undefined, expect: true, build: () => doc(slide) });
    }
    probes.push({ where: 'slide.operations = [100 items]', value: Array(100).fill(BASES.add_text), expect: true, build: v => doc(newSlide(v)) });
    probes.push({ where: 'slide.operations = [101 items]', value: Array(101).fill(BASES.add_text), expect: false, build: v => doc(newSlide(v)) });
    continue;
  }
  walk(child, `slide.${name}`, value => doc({ ...SLIDE_BASES[name], [name]: value }), name, 'slide');
}
// Top-level properties.
for (const [name, child] of Object.entries(root.properties) as [string, S][]) {
  if (name === 'slides') continue;
  walk(child, name, value => ({ ...doc(newSlide([BASES.add_text])), [name]: value }), name, 'input');
}
probes.push({ where: 'input + explanation', value: 'x', expect: false, build: v => ({ ...doc(newSlide([BASES.add_text])), explanation: v }) });
probes.push({ where: 'input without base_structure_fingerprint', value: 0, expect: false, build: () => {
  const { base_structure_fingerprint: _f, ...rest } = doc(newSlide([BASES.add_text])); return rest; } });
probes.push({ where: 'slides = [] ', value: 0, expect: false, build: () => ({ ...doc(newSlide([])), slides: [] }) });
probes.push({ where: 'slides = [4 items]', value: 0, expect: false, build: () => ({ ...doc(newSlide([])),
  slides: ['A', 'B', 'C', 'D'].map(slide_ref => ({ slide_ref, operations: [BASES.add_text] })) }) });

test('presentation_apply_patch takes exactly what its input_schema allows', () => {
  const mismatches: string[] = [];
  for (const probe of probes) {
    let ok = true; let message = '';
    try { parseScenePatch(probe.build(probe.value) as never, undefined); } catch (error) {
      if (!(error instanceof PatchRefused)) throw error;
      ok = false; message = error.message;
    }
    if (ok !== probe.expect) {
      mismatches.push(`${probe.expect ? 'SCHEMA ACCEPTS, CODE REJECTS' : 'SCHEMA REJECTS, CODE ACCEPTS'} | ${probe.where}${message ? ` | ${message}` : ''}`);
    }
  }
  expect(probes.length).toBeGreaterThan(600);
  expect(mismatches).toEqual([]);
});
