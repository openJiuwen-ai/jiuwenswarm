/**
 * The slide package tools' input_schema in tools.json against the pane: boundary values of every property the
 * schemas offer go in through the runtime on a fake deck, and what the pane refuses as INVALID_ARGUMENT must be what
 * the schema refuses. A field, bound or form on either side without the other fails here.
 */
import { expect, test } from 'vitest';

import tools from '@office/powerpoint/tools.json';

import { FakeDeck, uploadedDeck, type FakeDeckOptions, type FakeSlideState } from './fakePowerPoint';
import { HostWriteQueue } from './hostWriteQueue';
import type { PresentationToolName } from './powerPointApp';
import { powerPointPackageRuntime } from './powerPointPackageTools';
import { powerPointSlideDeck } from './powerPointWriteTools';
import { sha256Hex } from './presentationAssetClient';
import { SlideSnapshots } from './slideSnapshots';

type S = Record<string, any>;

const NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
  + 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
  + 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"';
const IMAGE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/image';
const PICTURE = `<p:pic ${NS}><p:nvPicPr><p:cNvPr id="7" name="Logo"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill>`
  + '<a:blip r:embed="rImage"/></p:blipFill><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="12700" cy="12700"/>'
  + '</a:xfrm></p:spPr></p:pic>';
const DOWNLOADS = { oa_deck: '/file-api/download?token=t' };

/** One valid spec of each form; a probe changes one field of it. Titles stand, so a subtitle is never alone. */
const FORMS: Record<string, S> = {
  column: { type: 'column', title: 't', categories: ['a', 'b'], values: [1, 2] },
  bar: { type: 'bar', title: 't', categories: ['a', 'b'], values: [1, 2] },
  line: { type: 'line', title: 't', categories: ['a', 'b'], series: [{ name: 's', values: [1, 2] }] },
  slope: { type: 'slope', title: 't', start_label: 'a', end_label: 'b', items: [{ name: 'x', start: 1, end: 2 },
    { name: 'y', start: 2, end: 1 }] },
  stacked_column: { type: 'stacked_column', title: 't', categories: ['a', 'b'], totals_number_format: '0',
    series: [{ name: 's', values: [1, 2] }, { name: 'u', values: [2, 1] }] },
  likert: { type: 'likert', title: 't', levels: ['a', 'b'], rows: [{ name: 'r', values: [40, 60] },
    { name: 'q', values: [50, 50] }] },
  waterfall: { type: 'waterfall', title: 't', start: { label: 's', value: 1 }, deltas: [{ label: 'd', value: 1 }],
    end_label: 'e', totals_number_format: '0' },
  scatter: { type: 'scatter', title: 't', points: [{ x: 0, y: 0 }, { x: 1, y: 1 }], x_axis: { max: 1, unit: 1 },
    y_axis: { max: 1, unit: 1 } },
  big_number: { type: 'big_number', title: '7%', subtitle: 'growth' },
};

/**
 * What the schema allows that no slide can take, so the pane refuses it as INVALID_ARGUMENT. Each names, in the
 * probe's ``where``, the rule the schema's prose or the deck states.
 */
const REFUSED: [RegExp, string][] = [
  [/^edit_built_chart spec\(big_number\)/, 'big_number is text, not a chart to draw again'],
  [/^edit_built_chart spec\(\w+\)\.position/, 'the chart stays in its frame; update_shape moves it'],
  [/spec\((column|bar)\)\.(categories|values) = \[30 items\]/, 'one value per category'],
  [/spec\(line\)\.(categories|series\[0\]\.values) = \[120 items\]/, 'one value per category'],
  [/spec\(stacked_column\)\.(categories = \[20|series\[0\]\.values = \[(1|30)) items\]/, 'one value per category'],
  [/spec\(likert\)\.(levels|rows\[0\]\.values) = \[10 items\]/, 'one value per level'],
  [/point_labels = \[30 items\]/, 'one label per point: thirty copies of one label share its index'],
  [/spec\(column\)\.growth_arrows = \[30 items\]/, 'an arrow runs between two of the categories'],
  [/spec\(column\)\.growth_arrows\[0\]\.to = 0/, 'an arrow runs between two of the categories'],
  [/spec\(waterfall\)\.deltas\[0\]\.value = -1000000000000/, 'the running total stays at 0 or more'],
];

interface Probe { tool: PresentationToolName; where: string; input: S; expect: boolean }

/** A deck with text on slide 256#, a picture on it, and slide 257#; the chart slide is made below. */
function deckOptions(): FakeDeckOptions {
  return { slides: [
    { id: '256#', shapes: [{ id: '2', type: 'TextBox', text: 'Atlasware' }, { id: '7', type: 'Image', xml: PICTURE }],
      parts: { 'ppt/media/image1.png': { contentType: 'image/png', base64: btoa('png'), relationshipId: 'rImage',
        relationshipType: IMAGE } } },
    { id: '257#', shapes: [{ id: '2', type: 'TextBox', text: 'Plan' }] },
  ] };
}

function runtimeOn(deck: FakeDeck, upload: Uint8Array<ArrayBuffer>) {
  const snapshots = new SlideSnapshots(powerPointSlideDeck(deck.run), new HostWriteQueue());
  return powerPointPackageRuntime({ run: deck.run, host: deck.host, journal: id => snapshots.journal(id),
    sandbox: async (_code, base64) => ({ base64, dirty: false }), forgetUndo: () => snapshots.forget(),
    fetcher: async () => new Response(upload) });
}

/** A string of ``length`` that matches ``pattern`` if one can (a number format takes zeros). */
const fill = (pattern: string | undefined, length: number) => ['0', 'x'].map(char => char.repeat(length))
  .find(text => !pattern || new RegExp(pattern).test(text)) ?? 'x'.repeat(length);

/** A value the schema node allows; for a key in ``known``, the one the deck has. */
function sample(schema: S, key: string, known: S): any {
  if (key in known) return known[key];
  if (schema.const !== undefined) return schema.const;
  if (schema.enum) return schema.enum[0];
  if (schema.anyOf) return sample(schema.anyOf[0], key, known);
  switch (schema.type) {
    case 'string': return schema.pattern && /\{6\}/.test(schema.pattern) ? '112233'
      : fill(schema.pattern, Math.max(1, schema.minLength ?? 1));
    case 'boolean': return false;
    case 'integer': case 'number': {
      const low = schema.minimum ?? (schema.exclusiveMinimum === undefined ? -Infinity : schema.exclusiveMinimum + 1);
      return Math.min(Math.max(0, low), schema.maximum ?? Infinity);
    }
    case 'array': return Array.from({ length: Math.max(1, schema.minItems ?? 1) }, () => sample(schema.items, key, known));
    case 'object': return Object.fromEntries((schema.required ?? [])
      .map((name: string) => [name, sample(schema.properties[name], name, known)]));
  }
  return 'x';
}

/** Boundary probes of one schema node; ``put`` places a value where the node is in an input the schema allows. */
function walk(probes: Probe[], tool: PresentationToolName, schema: S, where: string, put: (value: any) => S,
  key: string, known: S, current?: any): void {
  const add = (value: any, expect: boolean, label = JSON.stringify(value)): void => {
    probes.push({ tool, where: `${tool} ${where} = ${label}`, input: put(value), expect });
  };
  const isId = key in known;
  if (schema.const !== undefined) return add('NotConst', false);
  if (schema.enum) {
    for (const value of schema.enum) add(value, true);
    return add('NotInEnum', false);
  }
  if (schema.anyOf) {
    for (const branch of schema.anyOf) walk(probes, tool, branch, where, put, key, known, current);
    return;
  }
  switch (schema.type) {
    case 'null': return add(null, true);
    case 'string': {
      add(123, false);
      if (schema.minLength) add(fill(schema.pattern, schema.minLength - 1), false, `[${schema.minLength - 1} chars]`);
      if (schema.minLength && !isId) add(fill(schema.pattern, schema.minLength), true, `[${schema.minLength} chars]`);
      if (schema.maxLength !== undefined) {
        add(fill(schema.pattern, schema.maxLength), true, `[${schema.maxLength} chars]`);
        add(fill(schema.pattern, schema.maxLength + 1), false, `[${schema.maxLength + 1} chars]`);
      }
      const unmatched = ['!!not-matching!!', '<', '%'].find(text => schema.pattern && !new RegExp(schema.pattern).test(text));
      if (unmatched) add(unmatched, false);
      return;
    }
    case 'boolean':
      add(true, true);
      add(false, true);
      return add('true', false, '"true"');
    case 'integer': case 'number': {
      const step = schema.type === 'integer' ? 1 : 0.001;
      if (schema.minimum !== undefined) {
        add(schema.minimum, true);
        add(schema.minimum - step, false);
      }
      if (schema.exclusiveMinimum !== undefined) {
        add(schema.exclusiveMinimum + step, true);
        add(schema.exclusiveMinimum, false);
      }
      if (schema.maximum !== undefined) {
        add(schema.maximum, true);
        add(schema.maximum + step, false);
      }
      if (schema.type === 'integer') add((schema.minimum ?? 0) + 0.5, false);
      return add('1', false, '"1"');
    }
    case 'array': {
      const items: any[] = current ?? Array(Math.max(1, schema.minItems ?? 1)).fill(sample(schema.items, key, known));
      const [item] = items;
      add('x', false, '"x"');
      if (schema.minItems !== undefined) {
        add(Array(schema.minItems).fill(item), true, `[${schema.minItems} items]`);
        if (schema.minItems > 0) add(Array(schema.minItems - 1).fill(item), false, `[${schema.minItems - 1} items]`);
      }
      if (schema.maxItems !== undefined) {
        add(Array(schema.maxItems).fill(item), true, `[${schema.maxItems} items]`);
        add(Array(schema.maxItems + 1).fill(item), false, `[${schema.maxItems + 1} items]`);
      }
      // A probe changes the first item; the others stay as the base has them.
      walk(probes, tool, schema.items, `${where}[0]`, value => put(items.map((other, at) => (at === 0 ? value : other))),
        key, known, item);
      return;
    }
    case 'object': {
      add('x', false, '"x"');
      walkObject(probes, tool, schema, schema.properties ?? {}, where, put, current ?? sample(schema, key, known), known);
    }
  }
}

/** Probes of an object's fields, each against ``base``; ``properties`` are the schemas of its fields. */
function walkObject(probes: Probe[], tool: PresentationToolName, schema: S, properties: S, where: string,
  put: (value: any) => S, base: S, known: S): void {
  const add = (value: S, expect: boolean, label: string) =>
    probes.push({ tool, where: `${tool} ${where}${label}`, input: put(value), expect });
  for (const [name, child] of Object.entries(properties) as [string, S][]) {
    walk(probes, tool, child, `${where}.${name}`, value => put({ ...base, [name]: value }), name, known, base[name]);
  }
  for (const name of schema.required ?? []) {
    const { [name]: _gone, ...rest } = base;
    add(rest, false, ` without ${name}`);
  }
  add({ ...base, zz_unknown: 1 }, schema.additionalProperties !== false, ' + zz_unknown');
}

/** The probes of a chart spec: per form, its own fields and the shared ones it lists. */
function walkSpec(probes: Probe[], tool: PresentationToolName, spec: S, put: (value: S) => S, known: S): void {
  for (const form of spec.oneOf as S[]) {
    const type = form.properties.type.const as string;
    const properties = Object.fromEntries(Object.entries(form.properties as S).map(([name, child]) => [name,
      Object.keys(child).length ? child : spec.properties[name]]));
    walkObject(probes, tool, form, properties, `spec(${type})`, put, FORMS[type], known);
  }
}

test('the slide package tools take exactly what their input_schema allows', async () => {
  const upload = uploadedDeck(['One']);
  // The slide edit_built_chart edits: 257# with a chart, as create_slide_chart left it.
  const made = new FakeDeck(deckOptions());
  await runtimeOn(made, upload).execute({ callId: 'setup', toolName: 'create_slide_chart', downloads: {},
    input: { slide_id: '257#', spec: FORMS.column } });
  const slides: FakeSlideState[] = structuredClone(made.slides);
  const chartSlide = slides[1].id;
  const asset = { handle: 'oa_deck', purpose: 'input_pptx', mime_type: 'application/x', size: upload.length,
    sha256: await sha256Hex(upload) };
  const known: Record<string, S> = {
    read_slide_text: { ref: '256#:2' },
    edit_slide_text: { ref: '256#:2', code: '<a:p/>' },
    find_and_replace: { slide_ids: '256#' },
    duplicate_slide: { slide_id: '256#' },
    import_slides: { after_slide_id: '256#', asset },
    copy_image_between_slides: { from_slide_id: '256#', from_shape_id: '7', to_slide_id: chartSlide },
    read_chart_style: { slide_id: chartSlide },
    create_slide_chart: { slide_id: '256#' },
    edit_built_chart: { slide_id: chartSlide },
    edit_slide_xml: { slide_id: '256#' },
    edit_slide_chart: { slide_id: chartSlide },
    edit_slide_master: {},
  };
  const probes: Probe[] = [];
  for (const [name, ids] of Object.entries(known) as [PresentationToolName, S][]) {
    const schema = (tools as S[]).find(tool => tool.name === name)!.input_schema as S;
    const base = { ...sample(schema, '', ids), ...(schema.properties.spec ? { spec: FORMS.column } : {}) };
    for (const [field, child] of Object.entries(schema.properties) as [string, S][]) {
      if (field === 'spec') {
        walkSpec(probes, name, child, spec => ({ ...base, spec }), ids);
        continue;
      }
      walk(probes, name, child, field, value => ({ ...base, [field]: value }), field, ids, base[field]);
    }
    for (const field of schema.required ?? []) {
      const { [field]: _gone, ...rest } = base;
      probes.push({ tool: name, where: `${name} without ${field}`, input: rest, expect: false });
    }
    probes.push({ tool: name, where: `${name} + zz_unknown`, input: { ...base, zz_unknown: 1 },
      expect: schema.additionalProperties !== false });
    probes.push({ tool: name, where: `${name} base`, input: base, expect: true });
  }

  const mismatches: string[] = [];
  for (const probe of probes) {
    const refusal = REFUSED.find(([pattern]) => pattern.test(probe.where));
    const expected = probe.expect && !refusal;
    const deck = new FakeDeck({ slides: structuredClone(slides) });
    const response = await runtimeOn(deck, upload).execute({ callId: 'c', toolName: probe.tool, input: probe.input,
      downloads: DOWNLOADS });
    const taken = response.ok || response.error.code !== 'INVALID_ARGUMENT';
    if (taken !== expected) {
      const why = response.ok ? '' : ` | ${response.error.message}`;
      mismatches.push(`${expected ? 'ALLOWED, PANE REFUSES' : `REFUSED (${refusal?.[1] ?? 'schema'}), PANE TAKES`}`
        + ` | ${probe.where}${why}`);
    }
  }
  expect(probes.length).toBeGreaterThan(1500);
  expect(REFUSED.filter(([pattern]) => !probes.some(probe => pattern.test(probe.where)))).toEqual([]);
  expect(mismatches).toEqual([]);
}, 60_000);
