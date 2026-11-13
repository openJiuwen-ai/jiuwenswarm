/**
 * Slide packages: what ``exportAsBase64`` gives for one slide, a small .pptx whose ``ppt/slides/slide1.xml`` is the
 * slide, with its layout, master and theme. These pure functions read a package or return an edited one; the pane
 * puts an edited package back in place of the slide. An edit that does not hold (no such shape, a shape of the
 * wrong kind, paragraph XML that is not paragraphs) throws ``PackageRefused``, which leaves the deck as it was.
 */
import {
  DOMParser, onErrorStopParsing, XMLSerializer, type Document, type Element, type Node,
} from '@xmldom/xmldom';
import { strFromU8, strToU8, unzipSync, zipSync } from 'fflate';

import type { JsonObject } from './gatewayClient';
import {
  buildChart, buildChartShapes, buildGrowthArrows, CHART_NAMESPACE, CHART_RELATIONSHIP_PLACEHOLDER,
} from './presentationChart';

const A = 'http://schemas.openxmlformats.org/drawingml/2006/main';
const P = 'http://schemas.openxmlformats.org/presentationml/2006/main';
const R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
const PR = 'http://schemas.openxmlformats.org/package/2006/relationships';
const CT = 'http://schemas.openxmlformats.org/package/2006/content-types';
const CHART_CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.drawingml.chart+xml';
const TABLE_GRAPHIC_DATA = 'http://schemas.openxmlformats.org/drawingml/2006/table';
const EMU_PER_POINT = 12_700;
const MAX_PACKAGE_BYTES = 32 * 1024 * 1024;
const MAX_EXPANDED_BYTES = 128 * 1024 * 1024;
const MAX_PARTS = 4096;
const SHAPE_ELEMENTS = ['sp', 'graphicFrame', 'pic', 'cxnSp', 'grpSp'];

/** A package's parts by path. */
type Parts = Record<string, Uint8Array>;

export interface PackageEdit {
  base64: string;
  /** False when there was nothing to change; ``base64`` is then the package as it came. */
  changed: boolean;
  result: JsonObject;
}

/** The edit does not hold for this slide; nothing was changed. */
export class PackageRefused extends Error {}

function decode(base64: string): Uint8Array {
  return Uint8Array.from(atob(base64), char => char.charCodeAt(0));
}

function encode(bytes: Uint8Array): string {
  let binary = '';
  for (let at = 0; at < bytes.length; at += 32768) binary += String.fromCharCode(...bytes.subarray(at, at + 32768));
  return btoa(binary);
}

/** The parts of a package, refused when it inflates past the limits or names a part outside a plain OPC path. */
function loadPackage(base64: string): Parts {
  let count = 0;
  let expanded = 0;
  const names = new Set<string>();
  // Four base64 characters carry three bytes.
  if (base64.length > MAX_PACKAGE_BYTES / 3 * 4 + 4) throw new PackageRefused('The slide package is over 32 MB');
  let parts: Parts;
  try {
    parts = unzipSync(decode(base64), { filter: file => {
      count += 1;
      expanded += file.originalSize;
      if (count > MAX_PARTS || expanded > MAX_EXPANDED_BYTES || file.originalSize > MAX_PACKAGE_BYTES) {
        throw new PackageRefused('The slide package inflates past the limits');
      }
      const path = file.name.endsWith('/') ? file.name.slice(0, -1) : file.name;
      const lower = path.toLowerCase();
      const canonical = lower.split('/').map(segment => segment.trim().replace(/\.+$/, ''))
        .filter(segment => segment && segment !== '.').join('/');
      if (!path || names.has(lower) || /^\/|[\\%:]|[^\x20-\x7e]/.test(path) || canonical !== lower
        || lower.endsWith('.piece')) {
        throw new PackageRefused(`The slide package has an unsafe part path: ${JSON.stringify(file.name)}`);
      }
      names.add(lower);
      return !file.name.endsWith('/');
    } });
  } catch (error) {
    if (error instanceof PackageRefused) throw error;
    throw new PackageRefused(`The file is not a PowerPoint package: ${error instanceof Error ? error.message : error}`);
  }
  if (!parts['[Content_Types].xml'] || !parts['ppt/presentation.xml']) {
    throw new PackageRefused('The file is not a PowerPoint package');
  }
  return parts;
}

function savePackage(parts: Parts): string {
  return encode(zipSync(parts, { level: 6 }));
}

function parseText(xml: string, what: string): Document {
  try {
    return new DOMParser({ onError: onErrorStopParsing }).parseFromString(xml, 'application/xml');
  } catch (error) {
    throw new PackageRefused(`${what} is not well-formed XML: ${error instanceof Error ? error.message : error}`);
  }
}

function parseXml(parts: Parts, path: string): Document {
  if (!parts[path]) throw new PackageRefused(`The slide package has no ${path}`);
  return parseText(strFromU8(parts[path]), path);
}

function serialize(node: Node): string {
  return new XMLSerializer().serializeToString(node);
}

function directElements(node: Node): Element[] {
  return Array.from(node.childNodes).filter((child): child is Element => child.nodeType === 1);
}

function elements(root: Document | Element, namespace: string, localName: string): Element[] {
  return Array.from(root.getElementsByTagNameNS(namespace, localName));
}

function slidePart(parts: Parts): string {
  const matches = Object.keys(parts).filter(path => /^ppt\/slides\/slide\d+\.xml$/.test(path));
  if (matches.length !== 1) throw new PackageRefused(`The slide package has ${matches.length} slides, not one`);
  return matches[0];
}

function relationshipsPart(part: string): string {
  const slash = part.lastIndexOf('/');
  return `${part.slice(0, slash)}/_rels/${part.slice(slash + 1)}.rels`;
}

/** The part a relationship of ``source`` targets. */
function resolveTarget(source: string, target: string): string {
  const path: string[] = [];
  for (const segment of `${source.slice(0, source.lastIndexOf('/'))}/${target}`.split('/')) {
    if (segment === '..') path.pop();
    else if (segment && segment !== '.') path.push(segment);
  }
  return path.join('/');
}

function relationships(parts: Parts, part: string): Document {
  const path = relationshipsPart(part);
  return parts[path] ? parseXml(parts, path) : parseText(`<Relationships xmlns="${PR}"/>`, path);
}

function addRelationship(rels: Document, type: string, target: string, external = false): string {
  const used = new Set(elements(rels, PR, 'Relationship').map(item => item.getAttribute('Id')));
  let serial = 1;
  while (used.has(`rId${serial}`)) serial += 1;
  const relationship = rels.createElementNS(PR, 'Relationship');
  relationship.setAttribute('Id', `rId${serial}`);
  relationship.setAttribute('Type', type);
  relationship.setAttribute('Target', target);
  if (external) relationship.setAttribute('TargetMode', 'External');
  rels.documentElement!.appendChild(relationship);
  return `rId${serial}`;
}

function isExternal(relationship: Element): boolean {
  const mode = relationship.getAttribute('TargetMode');
  return mode !== null && mode.trim().toLowerCase() !== 'internal';
}

/** The top-level element of the shape whose ``cNvPr`` id is ``shapeId``. */
function shapeElement(slide: Document, shapeId: string): Element {
  const found = elements(slide, P, 'cNvPr').filter(element => element.getAttribute('id') === shapeId);
  if (found.length !== 1) {
    throw new PackageRefused(`Shape ${shapeId} is not on this slide; list_slide_shapes gives the shape ids`);
  }
  for (let node: Element | null = found[0]; node; node = node.parentElement) {
    if (node.namespaceURI === P && SHAPE_ELEMENTS.includes(node.localName ?? '')) return node;
  }
  throw new PackageRefused(`Shape ${shapeId} is not a shape`);
}

/** A shape of the slide itself, not one inside a group, whose position is the group's own. */
function isTopLevel(shape: Element): boolean {
  return shape.parentElement?.namespaceURI === P && shape.parentElement.localName === 'spTree';
}

function nextShapeId(slide: Document): number {
  return Math.max(0, ...elements(slide, P, 'cNvPr').map(element => Number(element.getAttribute('id')))
    .filter(Number.isInteger)) + 1;
}

function shapeTree(slide: Document): Element {
  const trees = elements(slide, P, 'spTree');
  if (trees.length !== 1) throw new PackageRefused('The slide has no single shape tree');
  return trees[0];
}

/** Shapes given as XML, appended to the slide's shape tree. */
function appendShapes(slide: Document, xmls: string[]): void {
  const tree = shapeTree(slide);
  for (const xml of xmls) {
    const fragment = parseText(`<root xmlns:p="${P}" xmlns:a="${A}" xmlns:c="${CHART_NAMESPACE}" xmlns:r="${R}">`
      + `${xml}</root>`, 'A built shape');
    tree.appendChild(slide.importNode(directElements(fragment.documentElement!)[0], true));
  }
}

/** The paragraphs of a shape's text, as read_slide_text gives them, or what the shape holds instead. */
export function readSlideText(base64: string, shapeId: string): string {
  const parts = loadPackage(base64);
  const shape = shapeElement(parseXml(parts, slidePart(parts)), shapeId);
  const paragraphs = (body: Element) => directElements(body)
    .filter(element => element.namespaceURI === A && element.localName === 'p').map(serialize).join('\n');
  switch (shape.localName) {
    case 'sp': {
      const body = elements(shape, P, 'txBody')[0];
      if (!body) {
        return '(empty: the shape has no text body, as an empty placeholder has none. edit_slide_text can write to it.)';
      }
      return paragraphs(body) || '(empty: the shape has a text body without paragraphs. edit_slide_text can write to it.)';
    }
    case 'cxnSp':
      return '(connector: no text body. Draw it again with presentation_apply_patch to change its route or style.)';
    case 'grpSp': {
      const children = elements(shape, P, 'sp');
      if (!children.length) return '(group: no text shapes inside, only pictures, connectors or groups without text.)';
      const content = children.map(child => {
        const properties = elements(child, P, 'cNvPr')[0];
        const body = elements(child, P, 'txBody')[0];
        return `--- child id="${properties?.getAttribute('id') ?? '?'}" (${properties?.getAttribute('name') ?? ''}) ---\n`
          + `${(body && paragraphs(body)) || '(empty)'}`;
      });
      return `(group: ${children.length} text shapes inside. Call edit_slide_text on a child id to change one.)\n\n`
        + content.join('\n\n');
    }
    case 'pic': {
      const alt = elements(shape, P, 'cNvPr')[0]?.getAttribute('descr');
      return `(image: no text body${alt ? `. Alt text: "${alt}"` : ''}. Move, resize or replace it with `
        + 'presentation_apply_patch update_shape or delete_shape.)';
    }
    default: {
      if (elements(shape, A, 'graphicData')[0]?.getAttribute('uri') === TABLE_GRAPHIC_DATA) {
        const cells = elements(shape, A, 'txBody');
        return `(table: ${cells.length} cells. find_and_replace changes the text in them.)\n\n`
          + cells.map((body, at) => `--- cell ${at + 1} ---\n${paragraphs(body)}`).join('\n\n');
      }
      return '(chart or SmartArt: not paragraph XML. See it with read_slide_image; read_chart_style reads a native '
        + 'chart\'s style, and edit_built_chart draws a chart again from a spec.)';
    }
  }
}

/**
 * Replaces a shape's paragraphs with ``paragraphXml``, keeping its ``bodyPr`` and ``lstStyle``; ``topLevel`` when
 * the shape is the slide's own, not one inside a group.
 */
export function editSlideText(base64: string, shapeId: string, paragraphXml: string):
  PackageEdit & { topLevel: boolean } {
  // No DTD at all, so no entity can expand or reach out.
  if (/<!DOCTYPE|<!ENTITY/i.test(paragraphXml)) throw new PackageRefused('The paragraph XML may not declare a DTD');
  const parts = loadPackage(base64);
  const path = slidePart(parts);
  const slide = parseXml(parts, path);
  const shape = shapeElement(slide, shapeId);
  if (shape.localName === 'grpSp') {
    const children = elements(shape, P, 'sp').map(child => elements(child, P, 'cNvPr')[0]?.getAttribute('id'));
    throw new PackageRefused(children.length
      ? `Shape ${shapeId} is a group without text of its own; call edit_slide_text on a child: ${children.join(', ')}`
      : `Shape ${shapeId} is a group without text shapes, so it has no text to edit`);
  }
  if (shape.localName !== 'sp') {
    throw new PackageRefused(`Shape ${shapeId} is a ${shape.localName} without a text body; read_slide_text says `
      + 'what it holds and what changes it');
  }
  const fragment = parseText(`<root xmlns:a="${A}" xmlns:r="${R}">${paragraphXml}</root>`, 'The paragraph XML');
  const paragraphs = directElements(fragment.documentElement!);
  if (!paragraphs.length || paragraphs.some(element => element.namespaceURI !== A || element.localName !== 'p')) {
    throw new PackageRefused('code must be <a:p> paragraphs and nothing else');
  }
  let body = elements(shape, P, 'txBody')[0];
  // The shape's list style is most often empty: a new run without rPr takes the master's default, not the old look.
  const hadFormatting = body ? hasFormattedRun(elements(body, A, 'p')) : false;
  if (body) {
    const kept = [elements(body, A, 'bodyPr')[0], elements(body, A, 'lstStyle')[0]];
    while (body.firstChild) body.removeChild(body.firstChild);
    kept.forEach(element => element && body!.appendChild(element));
  } else {
    body = slide.createElementNS(P, 'p:txBody');
    body.appendChild(slide.createElementNS(A, 'a:bodyPr'));
    body.appendChild(slide.createElementNS(A, 'a:lstStyle'));
    shape.appendChild(body);
  }
  for (const paragraph of paragraphs) body.appendChild(slide.importNode(paragraph, true));
  parts[path] = strToU8(serialize(slide));
  const dropped = hadFormatting && !hasFormattedRun(paragraphs);
  return { base64: savePackage(parts), changed: true, topLevel: isTopLevel(shape), result: dropped ? { warning: 'The '
    + 'text you replaced had run formatting (size, bold, italic, colour or font) that the new paragraphs leave out, '
    + 'and the shape has no list style to fall back on, so the new text takes the layout or master default. Copy '
    + '<a:rPr> from read_slide_text into the new runs to keep the look.' } : {} };
}

function hasFormattedRun(paragraphs: Element[]): boolean {
  return paragraphs.flatMap(paragraph => elements(paragraph, A, 'r')).some(run => {
    const properties = elements(run, A, 'rPr')[0];
    return !!properties && (['sz', 'b', 'i'].some(name => properties.hasAttribute(name))
      || elements(properties, A, 'solidFill').length > 0 || elements(properties, A, 'latin').length > 0);
  });
}

export interface ReplacementPair { find: string; replace: string }

export interface FindOptions {
  match: 'whole_word' | 'substring';
  case_sensitive: boolean;
  preserve_case: boolean;
  include_notes: boolean;
  dry_run: boolean;
}

/** Where a slide's matches are: a shape's text, a table cell, or the notes. */
export interface MatchLocation {
  container: 'body' | 'table' | 'notes';
  replacements: number;
  shape_id?: string;
  shape_name?: string;
  cell?: string;
}

/** Text find_and_replace found but leaves alone, and why. */
export interface SkippedMatch { reason: string; detail: string }

/** What find_and_replace found on one slide. */
export interface SlideMatches {
  replacements: number;
  perPair: number[];
  locations: MatchLocation[];
  skipped: SkippedMatch[];
  /** The top-level shapes whose text changed, whose frames may need to fit their text again. */
  changedShapeIds: string[];
  notesFound: boolean;
}

/** A run of text in a paragraph; a line break or a field cannot take part in a replacement. */
interface TextSegment { text: string; textElement?: Element; modified?: boolean }

function findPattern(find: string, options: FindOptions): RegExp {
  const escaped = find.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const source = options.match === 'substring' ? escaped : `(?<![A-Za-z0-9])${escaped}(?![A-Za-z0-9])`;
  return new RegExp(source, options.case_sensitive ? 'g' : 'gi');
}

function withCasingOf(source: string, replacement: string): string {
  if (source === source.toUpperCase()) return replacement.toUpperCase();
  if (source === source.toLowerCase()) return replacement.toLowerCase();
  if (source[0] === source[0].toUpperCase() && source.slice(1) === source.slice(1).toLowerCase()) {
    return replacement.slice(0, 1).toUpperCase() + replacement.slice(1);
  }
  return replacement;
}

function paragraphSegments(paragraph: Element): TextSegment[] {
  const segments: TextSegment[] = [];
  const visit = (parent: Element) => {
    for (const child of directElements(parent)) {
      if (child.namespaceURI !== A) continue;
      if (child.localName === 'br') segments.push({ text: '\n' });
      else if (child.localName === 'fld') segments.push({ text: '' });
      else if (child.localName === 't') segments.push({ text: child.textContent ?? '', textElement: child });
      else if (child.localName === 'r') visit(child);
    }
  };
  visit(paragraph);
  return segments;
}

/**
 * Replaces the pair's matches in the segments, last first: a match within one run keeps that run, one across runs
 * goes into the first of them. A match over a line break or a field is left and counted as ``blocked``.
 */
function replaceSegments(segments: TextSegment[], pair: ReplacementPair, options: FindOptions) {
  const joined = segments.map(segment => segment.text).join('');
  const offsets: number[] = [];
  let offset = 0;
  for (const segment of segments) {
    offsets.push(offset);
    offset += segment.text.length;
  }
  let count = 0;
  let blocked = 0;
  for (const match of [...joined.matchAll(findPattern(pair.find, options))].reverse()) {
    const start = match.index;
    const end = start + match[0].length;
    // A field takes no characters of the joined text: the match runs across it when it starts before and ends after.
    const affected = segments.flatMap((segment, at) => (segment.text.length
      ? offsets[at] < end && offsets[at] + segment.text.length > start : offsets[at] > start && offsets[at] < end)
      ? [at] : []);
    if (affected.some(at => !segments[at].textElement)) {
      blocked += 1;
      continue;
    }
    const replacement = options.preserve_case ? withCasingOf(match[0], pair.replace) : pair.replace;
    affected.forEach((at, position) => {
      const segment = segments[at];
      const from = Math.max(0, start - offsets[at]);
      const to = Math.min(segment.text.length, end - offsets[at]);
      const next = segment.text.slice(0, from) + (position === 0 ? replacement : '') + segment.text.slice(to);
      if (next !== segment.text) {
        segment.text = next;
        segment.modified = true;
      }
    });
    count += 1;
  }
  return { count, blocked };
}

function commitSegments(segments: TextSegment[]): boolean {
  let changed = false;
  for (const segment of segments) {
    if (!segment.modified || !segment.textElement) continue;
    changed = true;
    if (segment.text) {
      segment.textElement.textContent = segment.text;
      continue;
    }
    const run = segment.textElement.parentElement;
    run?.removeChild(segment.textElement);
    if (run?.localName === 'r' && !elements(run, A, 't').length) run.parentNode?.removeChild(run);
  }
  return changed;
}

function siblingIndex(element: Element, parent: Element, localName: string): number {
  return directElements(parent).filter(child => child.namespaceURI === A && child.localName === localName)
    .indexOf(element);
}

function paragraphLocation(paragraph: Element, notes: boolean): MatchLocation & { inGroup: boolean } {
  let container: MatchLocation['container'] = notes ? 'notes' : 'body';
  let shape: Element | undefined;
  let cell: Element | undefined;
  let inGroup = false;
  for (let parent = paragraph.parentElement; parent; parent = parent.parentElement) {
    if (parent.namespaceURI === A && parent.localName === 'tc') cell ??= parent;
    if (parent.namespaceURI === A && parent.localName === 'tbl' && !notes) container = 'table';
    if (parent.namespaceURI === P && parent.localName === 'grpSp') inGroup = true;
    if (parent.namespaceURI === P && [...SHAPE_ELEMENTS, 'contentPart'].includes(parent.localName ?? '')) shape ??= parent;
  }
  const row = cell?.parentElement;
  const properties = shape && elements(shape, P, 'cNvPr')[0];
  return { container, replacements: 0, inGroup,
    ...(properties ? { shape_id: properties.getAttribute('id') ?? '', shape_name: properties.getAttribute('name') ?? '' }
      : {}),
    ...(cell && row?.parentElement ? { cell: `r${siblingIndex(row, row.parentElement, 'tr')}`
      + `c${siblingIndex(cell, row, 'tc')}` } : {}) };
}

function replaceInDocument(document: Document, notes: boolean, pairs: ReplacementPair[], options: FindOptions,
  found: SlideMatches): boolean {
  const locations = new Map<string, MatchLocation>();
  let blocked = 0;
  let changed = false;
  for (const paragraph of elements(document, A, 'p')) {
    const segments = paragraphSegments(paragraph);
    if (!segments.length) continue;
    let count = 0;
    pairs.forEach((pair, index) => {
      const replaced = replaceSegments(segments, pair, options);
      found.perPair[index] += replaced.count;
      count += replaced.count;
      blocked += replaced.blocked;
    });
    if (!count) continue;
    found.replacements += count;
    const { inGroup, ...location } = paragraphLocation(paragraph, notes);
    const key = `${location.container}|${location.shape_id ?? ''}|${location.cell ?? ''}`;
    const entry = locations.get(key) ?? location;
    if (!locations.has(key)) {
      locations.set(key, entry);
      found.locations.push(entry);
    }
    entry.replacements += count;
    if (location.shape_id && !inGroup && !notes && !found.changedShapeIds.includes(location.shape_id)) {
      found.changedShapeIds.push(location.shape_id);
    }
    if (!options.dry_run && commitSegments(segments)) changed = true;
  }
  for (const field of elements(document, A, 'fld')) {
    const text = elements(field, A, 't').map(item => item.textContent ?? '').join('');
    for (const pair of pairs) {
      const count = [...text.matchAll(findPattern(pair.find, options))].length;
      if (count) {
        found.skipped.push({ reason: 'field-text', detail: `"${pair.find}" occurs ${count}x in a field PowerPoint `
          + 'fills in (a slide number, a date), which was left as it is' });
      }
    }
  }
  if (blocked) {
    found.skipped.push({ reason: 'line-break-span',
      detail: `${blocked} occurrences span a line break or a field and were not replaced` });
  }
  return changed;
}

/** The parts find_and_replace leaves, and what changes their text instead. */
const SKIPPED_PARTS = [
  { path: /^ppt\/charts\/chart(?:Ex)?\d+\.xml$/, reason: 'chart-text', label: 'chart',
    instead: 'edit_built_chart draws a chart again from a spec' },
  { path: /^ppt\/diagrams\/(?:data|drawing)\d+\.xml$/, reason: 'diagram-text', label: 'SmartArt',
    instead: 'the user can change it in PowerPoint' },
  { path: /^ppt\/slideLayouts\/slideLayout\d+\.xml$/, reason: 'layout-text', label: 'slide layout', template: true,
    instead: 'the user can change it in PowerPoint\'s slide master view' },
  { path: /^ppt\/slideMasters\/slideMaster\d+\.xml$/, reason: 'master-text', label: 'slide master', template: true,
    instead: 'the user can change it in PowerPoint\'s slide master view' },
];

/**
 * The layout the slide uses and that layout's master. The package carries every layout of the master; only these
 * show on the slide.
 */
function slideTemplates(parts: Parts, slide: string): Set<string> {
  const targets = (part: string, kind: string) => elements(relationships(parts, part), PR, 'Relationship')
    .filter(item => item.getAttribute('Type')?.endsWith(`/${kind}`))
    .map(item => resolveTarget(part, item.getAttribute('Target') ?? ''));
  const layouts = targets(slide, 'slideLayout');
  return new Set([...layouts, ...layouts.flatMap(layout => targets(layout, 'slideMaster'))]);
}

function decodeXmlText(value: string): string {
  const entities: Record<string, string> = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&apos;': '\'' };
  return value.replace(/&(?:amp|lt|gt|quot|apos);|&#x([0-9A-Fa-f]+);|&#(\d+);/g, (match, hex, decimal) => (hex
    ? String.fromCodePoint(Number.parseInt(hex, 16))
    : decimal ? String.fromCodePoint(Number.parseInt(decimal, 10)) : entities[match] ?? match));
}

/** Matches in the parts find_and_replace does not edit: charts, SmartArt, the slide's layout and its master. */
function skippedParts(parts: Parts, slide: string, pairs: ReplacementPair[], options: FindOptions): SkippedMatch[] {
  const templates = slideTemplates(parts, slide);
  return Object.keys(parts).flatMap(path => {
    const kind = SKIPPED_PARTS.find(item => item.path.test(path));
    if (!kind || (kind.template && !templates.has(path))) return [];
    const xml = strFromU8(parts[path]);
    const values = ['a:t', 'c:v', 'cx:v', 'cx:pt'].flatMap(tag => [...xml.matchAll(
      new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)</${tag}>`, 'g'))].map(match => decodeXmlText(match[1])));
    const text = values.join('\n');
    return pairs.flatMap(pair => {
      const count = [...text.matchAll(findPattern(pair.find, options))].length;
      return count ? [{ reason: kind.reason, detail: `"${pair.find}" occurs ${count}x in the ${kind.label} (${path}), `
        + `which find_and_replace does not edit; ${kind.instead}` }] : [];
    });
  });
}

/** find_and_replace on one slide and its notes; with ``dry_run`` it only counts. */
export function findAndReplace(base64: string, pairs: ReplacementPair[], options: FindOptions):
  { base64: string; changed: boolean; found: SlideMatches } {
  const parts = loadPackage(base64);
  const found: SlideMatches = { replacements: 0, perPair: pairs.map(() => 0), locations: [], skipped: [],
    changedShapeIds: [], notesFound: false };
  let changed = false;
  const slide = slidePart(parts);
  const documents = [{ path: slide, notes: false }];
  if (options.include_notes) {
    const notes = Object.keys(parts).filter(path => /^ppt\/notesSlides\/notesSlide\d+\.xml$/.test(path));
    found.notesFound = notes.length > 0;
    documents.push(...notes.map(path => ({ path, notes: true })));
  }
  for (const { path, notes } of documents) {
    const document = parseXml(parts, path);
    if (!replaceInDocument(document, notes, pairs, options, found)) continue;
    parts[path] = strToU8(serialize(document));
    changed = true;
  }
  found.skipped.push(...skippedParts(parts, slide, pairs, options));
  return changed ? { base64: savePackage(parts), changed, found } : { base64, changed, found };
}

/** Where copy_image_between_slides puts the copy, in points; each one left out keeps the source's. */
export interface ImageGeometry { x?: number; y?: number; width?: number; height?: number }

/** Each attribute in the relationships namespace (``r:embed``, ``r:link``, ``r:id``) on ``element`` and inside it. */
function relationshipReferences(element: Element): { element: Element; name: string; id: string }[] {
  return [element, ...Array.from(element.getElementsByTagName('*'))].flatMap(item => Array.from(item.attributes)
    .filter(attribute => attribute.namespaceURI === R)
    .map(attribute => ({ element: item, name: attribute.name, id: attribute.value })));
}

/**
 * Copies picture ``shapeId`` of the source slide onto the target slide, with every part and relationship it names
 * (its image, a video, a hyperlink) and their content types.
 */
export function copyImage(sourceBase64: string, targetBase64: string, shapeId: string,
  geometry: ImageGeometry): PackageEdit {
  const source = loadPackage(sourceBase64);
  const target = loadPackage(targetBase64);
  const sourcePath = slidePart(source);
  const targetPath = slidePart(target);
  const picture = shapeElement(parseXml(source, sourcePath), shapeId);
  if (picture.localName !== 'pic') throw new PackageRefused(`Shape ${shapeId} is not a picture`);
  if (!isTopLevel(picture)) {
    throw new PackageRefused(`Picture ${shapeId} is inside a group, where its position and size are the group's `
      + 'own, so it cannot be copied on its own');
  }
  const references = relationshipReferences(picture);
  if (!references.some(reference => reference.name.endsWith(':embed'))) {
    throw new PackageRefused(`Picture ${shapeId} has no embedded image`);
  }
  const sourceRels = new Map(elements(relationships(source, sourcePath), PR, 'Relationship')
    .map(item => [item.getAttribute('Id'), item]));
  const targetRels = relationships(target, targetPath);
  /** The target's relationship for each of the source's the picture names. */
  const renamed = new Map<string, string>();
  /** Each part copied, by its path in the source; a video and its media share one. */
  const copied = new Map<string, string>();
  for (const id of new Set(references.map(reference => reference.id))) {
    const relationship = sourceRels.get(id);
    if (!relationship) throw new PackageRefused(`Picture ${shapeId} names relationship ${id}, which the slide lacks`);
    const type = relationship.getAttribute('Type') ?? '';
    const link = relationship.getAttribute('Target') ?? '';
    if (isExternal(relationship)) {
      if (type !== `${R}/hyperlink`) {
        throw new PackageRefused(`Picture ${shapeId} links ${link}, outside the deck, which the pane does not copy`);
      }
      renamed.set(id, addRelationship(targetRels, type, link, true));
      continue;
    }
    const from = resolveTarget(sourcePath, link);
    if (!from.startsWith('ppt/media/') || !source[from]) {
      throw new PackageRefused(`Picture ${shapeId} names ${from}, which is not media of the slide`);
    }
    let to = copied.get(from);
    if (!to) {
      const [, stem, extension = '.bin'] = /^ppt\/media\/(.*?)\d*(\.[^.]*)?$/.exec(from.toLowerCase())!;
      let serial = 1;
      while (target[`ppt/media/${stem}${serial}${extension}`]) serial += 1;
      to = `ppt/media/${stem}${serial}${extension}`;
      target[to] = source[from].slice();
      copied.set(from, to);
    }
    renamed.set(id, addRelationship(targetRels, type, `../${to.slice('ppt/'.length)}`));
  }

  const slide = parseXml(target, targetPath);
  const copy = slide.importNode(picture, true);
  const id = String(nextShapeId(slide));
  const properties = elements(copy, P, 'cNvPr')[0];
  properties.setAttribute('id', id);
  properties.setAttribute('name', `Picture ${id}`);
  for (const reference of relationshipReferences(copy)) {
    reference.element.setAttributeNS(R, reference.name, renamed.get(reference.id)!);
  }
  // On the target, a placeholder would take the place and look of the target layout's placeholder of its index.
  elements(copy, P, 'ph').forEach(placeholder => placeholder.parentNode?.removeChild(placeholder));
  const transform = elements(copy, A, 'xfrm')[0];
  const offset = transform && directElements(transform).find(item => item.localName === 'off');
  const extent = transform && directElements(transform).find(item => item.localName === 'ext');
  if (!offset || !extent) throw new PackageRefused(`Picture ${shapeId} has no position and size`);
  const emu = (points: number) => String(Math.round(points * EMU_PER_POINT));
  if (geometry.x !== undefined) offset.setAttribute('x', emu(geometry.x));
  if (geometry.y !== undefined) offset.setAttribute('y', emu(geometry.y));
  if (geometry.width !== undefined) extent.setAttribute('cx', emu(geometry.width));
  if (geometry.height !== undefined) extent.setAttribute('cy', emu(geometry.height));
  shapeTree(slide).appendChild(copy);

  const sourceTypes = parseXml(source, '[Content_Types].xml');
  const targetTypes = parseXml(target, '[Content_Types].xml');
  for (const [from, to] of copied) {
    const extension = to.slice(to.lastIndexOf('.') + 1);
    const contentType = (elements(sourceTypes, CT, 'Override').find(item => item.getAttribute('PartName') === `/${from}`)
      ?? elements(sourceTypes, CT, 'Default').find(item => item.getAttribute('Extension')?.toLowerCase() === extension))
      ?.getAttribute('ContentType');
    if (!contentType) throw new PackageRefused(`The slide package gives no content type for ${from}`);
    const known = elements(targetTypes, CT, 'Default')
      .find(item => item.getAttribute('Extension')?.toLowerCase() === extension);
    if (known?.getAttribute('ContentType') === contentType) continue;
    // A target whose default for the extension is another type keeps it; the copy gets its own.
    const entry = targetTypes.createElementNS(CT, known ? 'Override' : 'Default');
    if (known) entry.setAttribute('PartName', `/${to}`);
    else entry.setAttribute('Extension', extension);
    entry.setAttribute('ContentType', contentType);
    targetTypes.documentElement!.appendChild(entry);
  }
  target[targetPath] = strToU8(serialize(slide));
  target[relationshipsPart(targetPath)] = strToU8(serialize(targetRels));
  target['[Content_Types].xml'] = strToU8(serialize(targetTypes));
  return { base64: savePackage(target), changed: true, result: { shape_id: id } };
}

/** The chart parts of a slide package in the order of their numbers, which is what ``chart_index`` counts. */
function chartParts(parts: Parts): string[] {
  const number = (path: string) => Number(/chart(\d+)\.xml$/.exec(path)![1]);
  return Object.keys(parts).filter(path => /^ppt\/charts\/chart\d+\.xml$/.test(path))
    .sort((first, second) => number(first) - number(second));
}

/** read_chart_style: each chart of the slide, as the fields create_slide_chart's spec takes. */
export function readChartStyle(base64: string): JsonObject[] {
  const parts = loadPackage(base64);
  return chartParts(parts).map(path => ({ chart_part: path, style: chartStyle(strFromU8(parts[path])) }));
}

function chartStyle(xml: string): JsonObject {
  const chartTypes = [...xml.matchAll(/<c:(\w+Chart)>/g)].map(match => match[1]);
  const [chartType] = chartTypes;
  const barDirection = /<c:barDir val="(col|bar)"/.exec(xml)?.[1];
  const grouping = /<c:grouping val="(\w+)"/.exec(xml)?.[1];
  const forms: Record<string, string> = { barChart: 'column', lineChart: 'line', scatterChart: 'scatter',
    pieChart: 'pie', doughnutChart: 'doughnut', areaChart: 'area' };
  let form: string;
  if (chartTypes.length > 1) form = `combo (${chartTypes.join('+')})`;
  else if (chartType === 'barChart') {
    form = barDirection === 'bar' ? (grouping === 'percentStacked' ? 'likert' : 'bar')
      : (grouping === 'stacked' || grouping === 'percentStacked' ? 'stacked_column' : 'column');
  } else form = forms[chartType ?? ''] ?? chartType ?? 'unknown';

  const distinct = (pattern: RegExp, map = (value: string) => value) =>
    [...new Set([...xml.matchAll(pattern)].map(match => map(match[1])))];
  const fonts = distinct(/typeface="([^"]+)"/g).filter(font => !font.startsWith('+'));
  const fills = distinct(/srgbClr val="([0-9A-Fa-f]{6})"/g, value => value.toUpperCase());
  const schemeColors = distinct(/schemeClr val="(\w+)"/g);
  const legendCode = /<c:legend>[\s\S]*?<c:legendPos val="(\w)"/.exec(xml)?.[1];
  const legend = xml.includes('<c:legend>')
    ? ({ r: 'right', t: 'top', b: 'bottom', l: 'left' } as Record<string, string>)[legendCode ?? 'r'] ?? 'right' : 'none';
  const valueAxes = [...xml.matchAll(/<c:valAx>[\s\S]*?<\/c:valAx>/g)].map(match => match[0]);
  const gapWidth = /<c:gapWidth val="(\d+)"/.exec(xml)?.[1];
  const allSeries = [...xml.matchAll(/<c:ser>[\s\S]*?<\/c:ser>/g)].map(match => match[0]);
  // A forecast is drawn as a second series of the line; it is not a series of the spec.
  const isForecast = (series: string) => /<c:v>[^<]*\(forecast\)<\/c:v>/.test(/<c:tx>[\s\S]*?<\/c:tx>/.exec(series)?.[0] ?? '');
  const series = chartType === 'lineChart' ? allSeries.filter(item => !isForecast(item)) : allSeries;
  const isScatter = chartType === 'scatterChart';
  const lineLike = chartType === 'lineChart' || isScatter;
  const withoutMarkers = (value: string) => value.replace(/<c:marker>[\s\S]*?<\/c:marker>|<c:marker\/>/g, '');
  const firstShapeStyle = (value: string) => /<c:spPr>[\s\S]*?<\/c:spPr>/.exec(value)?.[0] ?? '';
  const markerStyle = (value: string) => firstShapeStyle(/<c:marker>[\s\S]*?<\/c:marker>/.exec(value)?.[0] ?? '');
  const seriesHead = (value: string) => {
    const end = value.search(/<c:(?:dPt|dLbls|trendline|errBars|cat|val|xVal)\b/);
    return end >= 0 ? value.slice(0, end) : value;
  };
  const seriesStyle = (value: string) => (isScatter && markerStyle(seriesHead(value)))
    || firstShapeStyle(withoutMarkers(seriesHead(value)));
  const pointStyle = (value: string) => (isScatter && markerStyle(value)) || firstShapeStyle(withoutMarkers(value));
  const readFill = (value: string): { hex?: string; filled: boolean } => {
    const hex = (source?: string) => source && /<a:srgbClr val="([0-9A-Fa-f]{6})"/.exec(source)?.[1]?.toUpperCase();
    const lineAt = value.search(/<a:ln\b|<a:effectLst\b/);
    const fillRegion = lineAt >= 0 ? value.slice(0, lineAt) : value;
    const solidFill = /<a:solidFill>[\s\S]*?<\/a:solidFill>/.exec(fillRegion)?.[0];
    if (solidFill) return { hex: hex(solidFill) || undefined, filled: true };
    if (!lineLike || /<a:(?:pattFill|gradFill|blipFill|grpFill)\b/.test(fillRegion)) return { filled: false };
    const lineFill = /<a:solidFill>[\s\S]*?<\/a:solidFill>/.exec(/<a:ln\b[\s\S]*?<\/a:ln>/.exec(value)?.[0] ?? '')?.[0];
    return { hex: hex(lineFill) || undefined, filled: !!lineFill };
  };
  const points = (item: string) => [...item.matchAll(/<c:dPt>[\s\S]*?<\/c:dPt>/g)].map(match => readFill(pointStyle(match[0])));
  const seriesFills = [...new Set(series.flatMap(item => [readFill(seriesStyle(item)).hex,
    ...points(item).map(point => point.hex)].filter((hex): hex is string => !!hex)))];
  const accentedPoints = series.reduce((count, item) => count + points(item).filter(point => point.filled).length, 0);
  const styledSeries = series.filter(item => /<c:spPr>[\s\S]*?<a:(?:srgbClr|schemeClr)/.test(item)).length;
  const seriesColorPattern = series.length > 1 ? 'per_series' : accentedPoints > 0 ? 'one_accent'
    : styledSeries > 0 || schemeColors.length > 0 ? 'uniform' : 'unknown';

  const unsupported: string[] = [];
  if (chartTypes.length > 1) unsupported.push('combo chart (multiple chart types)');
  if (chartTypes.some(type => /3D/.test(type))) unsupported.push('3D chart');
  if (form !== 'scatter' && valueAxes.length > 1) unsupported.push('secondary value axis');
  if (['pie', 'doughnut', 'area', 'unknown'].includes(form)
    || (chartTypes.length === 1 && chartType !== 'barChart' && !forms[chartType])) unsupported.push(`${form} chart`);
  if (chartType === 'barChart' && series.length > 1 && (grouping ?? 'clustered') === 'clustered') {
    unsupported.push(`clustered ${barDirection === 'bar' ? 'bar' : 'column'} with ${series.length} series `
      + '(the builder draws one series)');
  }
  if (form === 'scatter' && series.length > 1) {
    unsupported.push(`scatter with ${series.length} series (the builder draws one series)`);
  }
  if (chartType === 'lineChart' && (grouping === 'stacked' || grouping === 'percentStacked')) {
    unsupported.push(`${grouping} line`);
  }
  if (chartType === 'barChart' && barDirection === 'col' && grouping === 'percentStacked') {
    unsupported.push('percentStacked column (the builder draws absolute stacks)');
  }
  for (const [tag, description] of [['trendline', 'trendline'], ['errBars', 'error bars'],
    ['upDownBars', 'up-down bars'], ['serLines', 'series lines'], ['dropLines', 'drop lines'],
    ['hiLowLines', 'high-low lines'], ['dTable', 'data table'], ['dispUnits', 'axis display units'],
    ['logBase', 'logarithmic axis'], ['dateAx', 'date axis']]) {
    if (new RegExp(`<c:${tag}\\b`).test(xml)) unsupported.push(description);
  }
  if (valueAxes.some(axis => /<c:orientation val="maxMin"/.test(axis))) unsupported.push('reversed value axis');
  const header = xml.slice(0, Math.max(0, xml.indexOf('<c:plotArea')));
  return {
    form, series_count: series.length, fonts, font_inherits_theme: fonts.length === 0, fills,
    series_fills: seriesFills, scheme_colors: schemeColors, legend,
    value_axis: valueAxes.some(axis => /<c:delete val="1"/.test(axis)) ? 'hidden' : 'shown',
    gridlines: /<c:majorGridlines\b/.test(xml),
    has_native_title: header.includes('<c:title>') && !/<c:autoTitleDeleted val="1"/.test(header),
    data_label_position: /<c:dLblPos val="(\w+)"/.exec(xml)?.[1] ?? null, series_color_pattern: seriesColorPattern,
    gap_width: gapWidth ? Number(gapWidth) : null, unsupported,
  };
}

/** create_slide_chart: the spec's chart part and frame (or big_number's text shapes) added to the slide. */
export function createChart(base64: string, spec: JsonObject): PackageEdit {
  const parts = loadPackage(base64);
  const path = slidePart(parts);
  const slide = parseXml(parts, path);
  const size = elements(parseXml(parts, 'ppt/presentation.xml'), P, 'sldSz')[0];
  if (!size) throw new PackageRefused('The slide package gives no slide size');
  const built = buildChartShapes(spec, nextShapeId(slide),
    { w: Number(size.getAttribute('cx')), h: Number(size.getAttribute('cy')) });
  let chartPart: string | undefined;
  let shapes = built.shapeXmls;
  if (built.chartXml !== null) {
    const number = Math.max(0, ...chartParts(parts).map(part => Number(/(\d+)\.xml$/.exec(part)![1]))) + 1;
    chartPart = `ppt/charts/chart${number}.xml`;
    parts[chartPart] = strToU8(built.chartXml);
    const rels = relationships(parts, path);
    const id = addRelationship(rels, `${R}/chart`, `../charts/chart${number}.xml`);
    parts[relationshipsPart(path)] = strToU8(serialize(rels));
    const types = parseXml(parts, '[Content_Types].xml');
    const override = types.createElementNS(CT, 'Override');
    override.setAttribute('PartName', `/${chartPart}`);
    override.setAttribute('ContentType', CHART_CONTENT_TYPE);
    types.documentElement!.appendChild(override);
    parts['[Content_Types].xml'] = strToU8(serialize(types));
    shapes = shapes.map(xml => xml.replace(CHART_RELATIONSHIP_PLACEHOLDER, id));
  }
  appendShapes(slide, shapes);
  parts[path] = strToU8(serialize(slide));
  const shapeIds = shapes.map(xml => /<p:cNvPr id="(\d+)"/.exec(xml)![1]);
  return { base64: savePackage(parts), changed: true,
    result: { shape_ids: shapeIds, ...(chartPart ? { chart_part: chartPart } : {}) } };
}

/** edit_built_chart: chart ``chartIndex`` of the slide drawn again from the spec, in its frame. */
export function editBuiltChart(base64: string, spec: JsonObject, chartIndex: number): PackageEdit {
  const parts = loadPackage(base64);
  const path = slidePart(parts);
  const charts = chartParts(parts);
  const chartPart = charts[chartIndex];
  if (!chartPart) {
    throw new PackageRefused(`chart_index ${chartIndex} is past the slide's ${charts.length} charts; `
      + 'read_chart_style lists them');
  }
  if (spec.type === 'big_number') {
    throw new PackageRefused('big_number is text, not a chart: delete the chart and create the big number instead');
  }
  if (spec.position !== undefined) {
    throw new PackageRefused('edit_built_chart keeps the chart where it is and takes no spec.position; move or '
      + 'resize the chart with presentation_apply_patch update_shape');
  }
  const { unsupported } = chartStyle(strFromU8(parts[chartPart])) as { unsupported: string[] };
  if (unsupported.length) {
    throw new PackageRefused(`The builder cannot draw this chart again (${unsupported.join('; ')}), so `
      + 'edit_built_chart leaves it as it is');
  }
  parts[chartPart] = strToU8(buildChart(spec));
  const slide = parseXml(parts, path);
  const id = elements(relationships(parts, path), PR, 'Relationship').find(item => !isExternal(item)
    && resolveTarget(path, item.getAttribute('Target') ?? '') === chartPart)?.getAttribute('Id');
  const frame = elements(slide, P, 'graphicFrame')
    .find(item => elements(item, CHART_NAMESPACE, 'chart')[0]?.getAttributeNS(R, 'id') === id);
  const frameId = frame && elements(frame, P, 'cNvPr')[0].getAttribute('id');
  if (frame && frameId) {
    // The arrows drawn for this chart go; the new spec draws its own.
    const arrows = new RegExp(`^GrowthArrow f${frameId} \\d+$`);
    for (const group of elements(slide, P, 'grpSp')) {
      if (arrows.test(elements(group, P, 'cNvPr')[0]?.getAttribute('name') ?? '')) group.parentNode?.removeChild(group);
    }
    if (spec.type === 'column' && (spec.growth_arrows as unknown[] | undefined)?.length) {
      const transform = elements(frame, P, 'xfrm')[0];
      const offset = transform && elements(transform, A, 'off')[0];
      const extent = transform && elements(transform, A, 'ext')[0];
      if (!offset || !extent) throw new PackageRefused('The chart frame has no position and size');
      const position = { x: Number(offset.getAttribute('x')), y: Number(offset.getAttribute('y')),
        w: Number(extent.getAttribute('cx')), h: Number(extent.getAttribute('cy')) };
      appendShapes(slide, buildGrowthArrows(spec, position, nextShapeId(slide), Number(frameId)));
    }
    parts[path] = strToU8(serialize(slide));
  }
  return { base64: savePackage(parts), changed: true, result: { chart_part: chartPart } };
}

/** The template facts read_presentation_context gives, from a slide package's first theme and master. */
export function themeOf(base64: string): {
  theme_palette: Record<string, string> | null;
  theme_fonts: { major: string; minor: string } | null;
  template_body_font_pt: number | null;
} {
  const parts = loadPackage(base64);
  const themeXml = parts['ppt/theme/theme1.xml'] ? strFromU8(parts['ppt/theme/theme1.xml']) : '';
  const masterXml = parts['ppt/slideMasters/slideMaster1.xml'] ? strFromU8(parts['ppt/slideMasters/slideMaster1.xml']) : '';
  const scheme = themeXml ? elements(parseText(themeXml, 'theme1.xml'), A, 'clrScheme')[0] : undefined;
  const palette: Record<string, string> = {};
  for (const key of ['dk1', 'dk2', 'lt1', 'lt2', 'accent1', 'accent2', 'accent3', 'accent4', 'accent5', 'accent6']) {
    const slot = scheme && elements(scheme, A, key)[0];
    const value = slot && (elements(slot, A, 'srgbClr')[0]?.getAttribute('val')
      || elements(slot, A, 'sysClr')[0]?.getAttribute('lastClr'));
    if (value) palette[key] = value.toUpperCase();
  }
  const major = /<a:majorFont>[\s\S]*?<a:latin typeface="([^"]+)"/.exec(themeXml)?.[1];
  const minor = /<a:minorFont>[\s\S]*?<a:latin typeface="([^"]+)"/.exec(themeXml)?.[1];
  const body = /<p:bodyStyle\b[^>]*>([\s\S]*?)<\/p:bodyStyle>/.exec(masterXml)?.[1] ?? '';
  const size = /<a:defRPr\b[^>]*\bsz="(\d+)"/.exec(/<a:lvl1pPr\b[^>]*>[\s\S]*?<\/a:lvl1pPr>/.exec(body)?.[0] ?? '')?.[1];
  return { theme_palette: Object.keys(palette).length ? palette : null,
    theme_fonts: major && minor ? { major, minor } : null, template_body_font_pt: size ? Number(size) / 100 : null };
}

/** What import_slides does not bring into the deck, by the last segment of a relationship's type. */
const UNIMPORTED = { oleObject: 'an OLE object', package: 'an embedded file', control: 'an ActiveX control' };

/**
 * The slides of a deck's package that import_slides inserts: those at 1-based ``positions`` (every slide when
 * undefined) in deck order, with their ids as ``insertSlidesFromBase64``'s ``sourceSlideIds`` takes them. Refused
 * when one of them, or a layout or master it uses, links a file outside the deck other than by a hyperlink, or holds
 * an OLE object, an ActiveX control or an embedded file.
 */
export function importedSlides(base64: string, positions?: number[]): { position: number; id: string }[] {
  let parts: Parts = {};
  try {
    // Only presentation.xml and the relationships are inflated: the deck may be far larger than a slide package.
    parts = unzipSync(decode(base64), { filter: file => (file.name === 'ppt/presentation.xml'
      || file.name.endsWith('.rels')) && file.originalSize <= MAX_PACKAGE_BYTES });
  } catch {
    // Not a zip; refused below.
  }
  if (!parts['ppt/presentation.xml']) throw new PackageRefused('The file is not a PowerPoint deck');
  const slides = elements(parseXml(parts, 'ppt/presentation.xml'), P, 'sldId');
  const chosen = positions === undefined ? slides.map((_slide, at) => at + 1)
    : [...new Set(positions)].sort((first, second) => first - second);
  const past = chosen.find(position => position > slides.length);
  if (past) throw new PackageRefused(`source_positions has ${past}, but the .pptx has ${slides.length} slides`);
  const slideParts = new Map(elements(relationships(parts, 'ppt/presentation.xml'), PR, 'Relationship')
    .map(item => [item.getAttribute('Id'), resolveTarget('ppt/presentation.xml', item.getAttribute('Target') ?? '')]));
  const checked = new Set<string>();
  const check = (part: string, position: number) => {
    if (checked.has(part)) return;
    checked.add(part);
    const where = part.startsWith('ppt/slides/') ? `Slide ${position} of the .pptx`
      : `${part}, which slide ${position} of the .pptx uses,`;
    for (const relationship of elements(relationships(parts, part), PR, 'Relationship')) {
      const type = relationship.getAttribute('Type') ?? '';
      const kind = type.slice(type.lastIndexOf('/') + 1);
      const link = relationship.getAttribute('Target') ?? '';
      if (isExternal(relationship)) {
        if (kind === 'hyperlink') continue;
        throw new PackageRefused(`${where} links ${link} outside the file, which import_slides does not bring in`);
      }
      const target = resolveTarget(part, link);
      const held = UNIMPORTED[kind as keyof typeof UNIMPORTED]
        ?? (/^ppt\/(?:embeddings|activeX)\//.test(target) ? 'an embedded object' : undefined);
      if (held) throw new PackageRefused(`${where} holds ${held} (${target}), which import_slides does not bring in`);
      if (kind === 'slideLayout' || kind === 'slideMaster') check(target, position);
    }
  };
  return chosen.map(position => {
    const slide = slides[position - 1];
    const id = slide.getAttribute('id') ?? '';
    if (!/^\d+$/.test(id)) throw new PackageRefused(`The deck names a slide id ${JSON.stringify(id)}`);
    const part = slideParts.get(slide.getAttributeNS(R, 'id'));
    if (!part) throw new PackageRefused(`Slide ${position} of the .pptx names no slide part`);
    check(part, position);
    return { position, id: `${id}#` };
  });
}
