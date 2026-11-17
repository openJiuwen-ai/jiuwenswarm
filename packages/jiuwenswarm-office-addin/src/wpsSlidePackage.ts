/**
 * WPS has no per-slide OOXML export. ``extractSlidePackage`` turns a saved copy of the whole deck
 * (``Presentation.SaveCopyAs``) into what PowerPoint's ``slide.exportAsBase64`` returns, so the slide package
 * tools run unchanged: one slide at ``ppt/slides/slide1.xml`` with the deck's masters, layouts and theme, every
 * other slide and whatever only they reach dropped. A link to another slide (a jump to it) has no slide to land on
 * in the package, so it goes too.
 */
import { DOMParser, XMLSerializer, type Document, type Element } from '@xmldom/xmldom';
import { strFromU8, strToU8, unzipSync, zipSync } from 'fflate';

import { base64Of, type Parts } from './presentationPackage';

const P = 'http://schemas.openxmlformats.org/presentationml/2006/main';
const R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
const PR = 'http://schemas.openxmlformats.org/package/2006/relationships';
const CT = 'http://schemas.openxmlformats.org/package/2006/content-types';
const SLIDE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide';
const PRESENTATION = 'ppt/presentation.xml';
const CONTENT_TYPES = '[Content_Types].xml';
const ISOLATED_SLIDE = 'ppt/slides/slide1.xml';

/**
 * The slide at 1-based ``position`` of a whole deck (``unzipDeck``), as a single-slide package (base64). ``deck``
 * stays as it is, so one unzipped copy serves every slide of a call.
 */
export function extractSlidePackage(deck: Readonly<Parts>, position: number): string {
  const parts = { ...deck };
  const presentation = parse(parts, PRESENTATION);
  const presentationRels = parse(parts, relsOf(PRESENTATION));
  const slideIds = elements(presentation, P, 'sldId');
  if (!Number.isInteger(position) || position < 1 || position > slideIds.length) {
    throw new Error(`Slide position ${position} is outside the saved copy (${slideIds.length} slides)`);
  }
  const relationships = new Map(elements(presentationRels, PR, 'Relationship')
    .map(node => [node.getAttribute('Id') ?? '', node]));
  const kept = slideIds[position - 1];
  const keptRelationship = relationships.get(kept.getAttributeNS(R, 'id') ?? '');
  if (!keptRelationship) throw new Error('The saved copy does not link the requested slide');
  const keptPart = resolve(PRESENTATION, keptRelationship.getAttribute('Target') ?? '');

  for (const slideId of slideIds) {
    if (slideId === kept) continue;
    const relationship = relationships.get(slideId.getAttributeNS(R, 'id') ?? '');
    relationship?.parentNode?.removeChild(relationship);
    slideId.parentNode?.removeChild(slideId);
  }
  // Custom shows and sections name slide ids that are no longer there.
  for (const localName of ['custShowLst', 'sectionLst']) {
    for (const node of Array.from(presentation.getElementsByTagNameNS('*', localName))) {
      const holder = localName === 'sectionLst' ? closestExt(node) ?? node : node;
      holder.parentNode?.removeChild(holder);
    }
  }
  parts[PRESENTATION] = serialize(presentation);
  parts[relsOf(PRESENTATION)] = serialize(presentationRels);

  const reachable = reachableParts(parts, keptPart);
  for (const path of Object.keys(parts)) {
    if (path !== CONTENT_TYPES && !reachable.has(path)) delete parts[path];
  }
  dropLinksToOtherSlides(parts, keptPart);
  if (keptPart !== ISOLATED_SLIDE) renamePart(parts, keptPart, ISOLATED_SLIDE);
  pruneContentTypes(parts);
  return base64Of(zipSync(parts, { level: 6 }));
}

/** The slide ids of a deck's package in order, as ``insertSlidesFromBase64`` takes them (``nnn#``). */
export function packageSlideIds(deck: Uint8Array): string[] {
  const parts = unzipSync(deck, { filter: file => file.name === PRESENTATION });
  return elements(parse(parts, PRESENTATION), P, 'sldId').map(node => `${node.getAttribute('id')}#`);
}

/** A saved copy of the whole deck, as its parts. */
export function unzipDeck(deck: Uint8Array): Parts {
  const parts = unzipSync(deck, { filter: file => !file.name.endsWith('/') });
  if (!parts[CONTENT_TYPES] || !parts[PRESENTATION]) throw new Error('The saved copy is not a PowerPoint package');
  return parts;
}

/** Every part linked from the package root, following relationships but not links to slides other than ``slide``. */
function reachableParts(parts: Parts, slide: string): Set<string> {
  const reachable = new Set<string>(['_rels/.rels']);
  const queue = targetsOf(parts, '', slide);
  while (queue.length) {
    const part = queue.shift()!;
    if (reachable.has(part) || !parts[part]) continue;
    reachable.add(part);
    const rels = relsOf(part);
    if (!parts[rels]) continue;
    reachable.add(rels);
    queue.push(...targetsOf(parts, part, slide));
  }
  return reachable;
}

function targetsOf(parts: Parts, part: string, slide: string): string[] {
  if (!parts[relsOf(part)]) return [];
  return internalRelationships(parse(parts, relsOf(part)), part)
    .filter(link => !linksOtherSlide(link, slide)).map(link => link.target);
}

/** Takes out each relationship to a slide other than ``slide`` and the elements that name it. */
function dropLinksToOtherSlides(parts: Parts, slide: string): void {
  for (const rels of Object.keys(parts).filter(path => path.endsWith('.rels'))) {
    const source = sourceOfRels(rels);
    const document = parse(parts, rels);
    const dropped = new Set(internalRelationships(document, source).filter(link => linksOtherSlide(link, slide))
      .map(({ node }) => {
        node.parentNode?.removeChild(node);
        return node.getAttribute('Id');
      }));
    if (!dropped.size) continue;
    parts[rels] = serialize(document);
    const xml = parse(parts, source);
    for (const node of Array.from(xml.getElementsByTagName('*'))) {
      if (dropped.has(node.getAttributeNS(R, 'id'))) node.parentNode?.removeChild(node);
    }
    parts[source] = serialize(xml);
  }
}

function linksOtherSlide(link: { node: Element; target: string }, slide: string): boolean {
  return link.node.getAttribute('Type') === SLIDE && link.target !== slide;
}

/** A part's relationships to other parts of the package, each with the part it targets. */
function internalRelationships(rels: Document, source: string): { node: Element; target: string }[] {
  return elements(rels, PR, 'Relationship').filter(node => node.getAttribute('TargetMode') !== 'External')
    .map(node => ({ node, target: resolve(source, node.getAttribute('Target') ?? '') }));
}

/** Moves ``from`` to ``to``, with its relationships and every relationship that points at it. */
function renamePart(parts: Parts, from: string, to: string): void {
  if (parts[to]) throw new Error(`Cannot isolate the slide: ${to} is still in use`);
  parts[to] = parts[from];
  delete parts[from];
  if (parts[relsOf(from)]) {
    parts[relsOf(to)] = parts[relsOf(from)];
    delete parts[relsOf(from)];
  }
  for (const rels of Object.keys(parts).filter(path => path.endsWith('.rels'))) {
    const source = sourceOfRels(rels);
    const document = parse(parts, rels);
    const moved = internalRelationships(document, source).filter(link => link.target === from);
    for (const { node } of moved) node.setAttribute('Target', relative(source, to));
    if (moved.length) parts[rels] = serialize(document);
  }
  const types = parse(parts, CONTENT_TYPES);
  for (const node of elements(types, CT, 'Override')) {
    if (node.getAttribute('PartName') === `/${from}`) node.setAttribute('PartName', `/${to}`);
  }
  parts[CONTENT_TYPES] = serialize(types);
}

function pruneContentTypes(parts: Parts): void {
  const types = parse(parts, CONTENT_TYPES);
  for (const node of elements(types, CT, 'Override')) {
    if (!parts[(node.getAttribute('PartName') ?? '').replace(/^\//, '')]) node.parentNode?.removeChild(node);
  }
  parts[CONTENT_TYPES] = serialize(types);
}

function closestExt(node: Element): Element | undefined {
  let current = node.parentNode as Element | null;
  while (current) {
    if (current.localName === 'ext') return current;
    current = current.parentNode as Element | null;
  }
  return undefined;
}

function relsOf(part: string): string {
  if (!part) return '_rels/.rels';
  const slash = part.lastIndexOf('/');
  return `${part.slice(0, slash + 1)}_rels/${part.slice(slash + 1)}.rels`;
}

function sourceOfRels(rels: string): string {
  if (rels === '_rels/.rels') return '';
  const match = /^(.*?)_rels\/([^/]+)\.rels$/.exec(rels);
  return match ? `${match[1]}${match[2]}` : '';
}

function resolve(source: string, target: string): string {
  if (target.startsWith('/')) return normalize(target.slice(1));
  const base = source.includes('/') ? source.slice(0, source.lastIndexOf('/')) : '';
  return normalize(base ? `${base}/${target}` : target);
}

function relative(source: string, target: string): string {
  const from = source.includes('/') ? source.slice(0, source.lastIndexOf('/')).split('/') : [];
  const to = target.split('/');
  let common = 0;
  while (common < from.length && common < to.length - 1 && from[common] === to[common]) common += 1;
  return [...from.slice(common).map(() => '..'), ...to.slice(common)].join('/');
}

function normalize(path: string): string {
  const output: string[] = [];
  for (const segment of path.split('/')) {
    if (!segment || segment === '.') continue;
    if (segment === '..') output.pop();
    else output.push(segment);
  }
  return output.join('/');
}

function parse(parts: Parts, path: string): Document {
  if (!parts[path]) throw new Error(`The saved copy is missing ${path}`);
  return new DOMParser().parseFromString(strFromU8(parts[path]), 'application/xml');
}

function serialize(document: Document): Uint8Array {
  return strToU8(new XMLSerializer().serializeToString(document));
}

function elements(root: Document | Element, namespace: string, localName: string): Element[] {
  return Array.from(root.getElementsByTagNameNS(namespace, localName));
}
