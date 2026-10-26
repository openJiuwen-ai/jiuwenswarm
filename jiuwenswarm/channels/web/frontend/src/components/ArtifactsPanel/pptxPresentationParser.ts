import JSZip from 'jszip';
import { SaxesParser } from 'saxes';
import { OoxmlArchiveLimitError, PRESENTATION_ARCHIVE_LIMITS, inspectOoxmlArchive, isOoxmlArchiveLimitError } from './ooxmlArchiveLimits';
import {
  MAX_PRESENTATION_UNCOMPRESSED_BYTES,
  type PresentationBounds,
  type PresentationChart,
  type PresentationColor,
  type PresentationData,
  type PresentationFill,
  type PresentationImage,
  type PresentationNode,
  type PresentationParagraph,
  type PresentationRun,
  type PresentationShape,
  type PresentationSlide,
  type PresentationSpacing,
  type PresentationStroke,
  type PresentationTable,
  type PresentationText,
} from './pptxPreviewModel';

type XmlNode = { name: string; attrs: Record<string, string>; children: XmlNode[]; text: string };
type Relationship = { id: string; target: string; type: string; external: boolean };
type ThemeFont = {
  latin?: string;
  eastAsian?: string;
  complexScript?: string;
  supplemental: Map<string, string>;
};
type Theme = { colors: Map<string, string>; majorFont: ThemeFont; minorFont: ThemeFont };
type Part = { path: string; xml: XmlNode; rels: Map<string, Relationship> };
type SlidePart = { part: Part };
type Transform = { scaleX: number; scaleY: number; offsetX: number; offsetY: number };
type Template = { background: PresentationFill; nodes: PresentationNode[]; placeholders: Map<string, PresentationShape> };
type TableStyle = { wholeTbl?: PresentationFill; band1H?: PresentationFill; firstRow?: PresentationFill };

const EMUS_PER_PIXEL = 9_525;
const CSS_PIXELS_PER_POINT = 96 / 72;
const EMPTY_FILL: PresentationFill = { kind: 'none' };
const WHITE_FILL: PresentationFill = { kind: 'solid', color: '#FFFFFF' };
const ROOT_TRANSFORM: Transform = { scaleX: 1, scaleY: 1, offsetX: 0, offsetY: 0 };
const SUPPORTED_GEOMETRIES = new Set([
  'rect',
  'roundRect',
  'ellipse',
  'triangle',
  'rtTriangle',
  'diamond',
  'parallelogram',
  'trapezoid',
  'hexagon',
  'pentagon',
  'chevron',
  'plus',
  'minus',
  'line',
  'lineVertical',
]);
const MEDIUM_STYLE_2_ACCENT_1 = '{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}';

export async function parsePresentation(buffer: ArrayBuffer): Promise<PresentationData> {
  inspectOoxmlArchive(buffer, PRESENTATION_ARCHIVE_LIMITS);
  const archive = await JSZip.loadAsync(buffer, { checkCRC32: true, createFolders: false });
  const entries = Object.values(archive.files).filter(entry => !entry.dir);
  if (entries.length === 0) throw new Error('The presentation archive is empty');

  const files = new Map<string, JSZip.JSZipObject>();
  for (const entry of entries) {
    if (entry.name.includes('..') || entry.name.startsWith('/')) throw new Error('The presentation contains an unsafe archive path');
    files.set(entry.name, entry);
  }
  const presentationFile = files.get('ppt/presentation.xml');
  if (!presentationFile) throw new Error('This is not a PPTX or PPTM presentation');

  let unpackedBytes = 0;
  const textCache = new Map<string, string>();
  const xmlCache = new Map<string, XmlNode>();
  const blobCache = new Map<string, Blob>();
  const readText = async (path: string): Promise<string | undefined> => {
    if (textCache.has(path)) return textCache.get(path);
    const entry = files.get(path);
    if (!entry) return undefined;
    const text = await entry.async('string');
    unpackedBytes += new TextEncoder().encode(text).byteLength;
    ensureUncompressedLimit(unpackedBytes);
    textCache.set(path, text);
    return text;
  };
  const readXml = async (path: string): Promise<XmlNode | undefined> => {
    if (xmlCache.has(path)) return xmlCache.get(path);
    const text = await readText(path);
    if (text === undefined) return undefined;
    const xml = parseXml(text);
    xmlCache.set(path, xml);
    return xml;
  };
  const readBlob = async (path: string): Promise<Blob | undefined> => {
    if (blobCache.has(path)) return blobCache.get(path);
    const entry = files.get(path);
    if (!entry) return undefined;
    const bytes = await entry.async('uint8array');
    unpackedBytes += bytes.byteLength;
    ensureUncompressedLimit(unpackedBytes);
    const blob = new Blob([new Uint8Array(bytes)], { type: mediaMimeType(path) });
    blobCache.set(path, blob);
    return blob;
  };

  const presentationXml = await readXml('ppt/presentation.xml');
  const presentationRels = await readRelationships('ppt/_rels/presentation.xml.rels', readXml);
  if (!presentationXml) throw new Error('The presentation XML cannot be read');
  const { width, height } = readSlideSize(presentationXml);
  const themeCache = new Map<string, Theme>();
  const partCache = new Map<string, Part>();
  const templateCache = new Map<string, Template>();

  const getPart = async (path: string): Promise<Part | undefined> => {
    if (partCache.has(path)) return partCache.get(path);
    const xml = await readXml(path);
    if (!xml) return undefined;
    const part: Part = { path, xml, rels: await readRelationships(relsPath(path), readXml) };
    partCache.set(path, part);
    return part;
  };
  const getSlidePart = async (path: string): Promise<SlidePart | undefined> => {
    const text = await readText(path);
    if (text === undefined) return undefined;
    return {
      part: { path, xml: parseXml(text), rels: await readRelationships(relsPath(path), readXml) },
    };
  };
  const getTheme = async (master: Part | undefined): Promise<Theme> => {
    if (!master) return emptyTheme();
    const themeRelationship = findRelationship(master.rels, 'theme');
    if (!themeRelationship || themeRelationship.external) return emptyTheme();
    const path = resolveTarget(master.path, themeRelationship.target);
    if (themeCache.has(path)) return themeCache.get(path)!;
    const xml = await readXml(path);
    const theme = xml ? parseTheme(xml) : emptyTheme();
    themeCache.set(path, theme);
    return theme;
  };
  const getTemplate = async (part: Part, theme: Theme): Promise<Template> => {
    const cached = templateCache.get(part.path);
    if (cached) return cached;
    const template = await parseTemplate(part, theme, readBlob, readXml);
    templateCache.set(part.path, template);
    return template;
  };

  const slideIdList = direct<XmlNode | undefined>(child(presentationXml, 'sldIdLst'));
  const slideReferences = direct<XmlNode[]>(child(slideIdList, 'sldId', true));
  const slides: PresentationSlide[] = [];
  for (let index = 0; index < slideReferences.length; index += 1) {
    const relationId = attr(slideReferences[index], 'r:id');
    const relationship = relationId ? presentationRels.get(relationId) : undefined;
    if (!relationship || relationship.external || !relationship.type.includes('slide')) {
      slides.push(invalidSlide(index));
      continue;
    }
    try {
      const slidePart = await getSlidePart(resolveTarget('ppt/presentation.xml', relationship.target));
      if (!slidePart) {
        slides.push(invalidSlide(index));
        continue;
      }
      const layoutRelationship = findRelationship(slidePart.part.rels, 'slideLayout');
      const layoutPart =
        layoutRelationship && !layoutRelationship.external ? await getPart(resolveTarget(slidePart.part.path, layoutRelationship.target)) : undefined;
      const masterRelationship = layoutPart ? findRelationship(layoutPart.rels, 'slideMaster') : undefined;
      const masterPart =
        masterRelationship && !masterRelationship.external ? await getPart(resolveTarget(layoutPart!.path, masterRelationship.target)) : undefined;
      const theme = await getTheme(masterPart);
      const masterTemplate = masterPart ? await getTemplate(masterPart, theme) : emptyTemplate();
      const layoutTemplate = layoutPart ? await getTemplate(layoutPart, theme) : emptyTemplate();
      slides.push(await parseSlide(slidePart.part, index, theme, masterTemplate, layoutTemplate, readBlob, readXml));
    } catch (error) {
      if (isOoxmlArchiveLimitError(error)) throw error;
      slides.push(invalidSlide(index));
    }
  }
  if (slides.length === 0) throw new Error('The presentation contains no readable slides');
  return { width, height, slides };
}

function ensureUncompressedLimit(size: number): void {
  if (size > MAX_PRESENTATION_UNCOMPRESSED_BYTES) throw new OoxmlArchiveLimitError('uncompressed-size');
}

function invalidSlide(index: number): PresentationSlide {
  return {
    id: `slide-${index + 1}`,
    name: `Slide ${index + 1}`,
    background: WHITE_FILL,
    nodes: [],
    status: 'invalid',
  };
}

function parseXml(input: string): XmlNode {
  const root: XmlNode = { name: '__root__', attrs: {}, children: [], text: '' };
  const stack = [root];
  const parser = new SaxesParser<{ xmlns: false }>({ xmlns: false });
  parser.on('opentag', tag => {
    const attrs: Record<string, string> = {};
    Object.entries(tag.attributes).forEach(([name, value]) => {
      attrs[name] = String(value);
    });
    const node: XmlNode = { name: tag.name, attrs, children: [], text: '' };
    stack[stack.length - 1].children.push(node);
    stack.push(node);
  });
  parser.on('text', text => {
    stack[stack.length - 1].text += text;
  });
  parser.on('cdata', text => {
    stack[stack.length - 1].text += text;
  });
  parser.on('closetag', () => {
    if (stack.length > 1) stack.pop();
  });
  parser.on('error', error => {
    throw new Error(`Invalid presentation XML: ${error.message}`);
  });
  parser.write(input).close();
  if (root.children.length !== 1) throw new Error('Invalid presentation XML document');
  return root.children[0];
}

function localName(name: string): string {
  return name.includes(':') ? name.slice(name.lastIndexOf(':') + 1) : name;
}

function attr(node: XmlNode | undefined, name: string): string | undefined {
  if (!node) return undefined;
  return node.attrs[name] ?? node.attrs[localName(name)] ?? Object.entries(node.attrs).find(([key]) => localName(key) === localName(name))?.[1];
}

function child(node: XmlNode | undefined, name: string, all = false): XmlNode | XmlNode[] | undefined {
  const matches = node?.children.filter(item => localName(item.name) === name) ?? [];
  return all ? matches : matches[0];
}

function descendants(node: XmlNode | undefined, name: string): XmlNode[] {
  if (!node) return [];
  const result: XmlNode[] = [];
  const visit = (current: XmlNode) => {
    current.children.forEach(item => {
      if (localName(item.name) === name) result.push(item);
      visit(item);
    });
  };
  visit(node);
  return result;
}

function direct<T>(value: XmlNode | XmlNode[] | undefined): T {
  return value as T;
}

function relsPath(path: string): string {
  const slash = path.lastIndexOf('/');
  const directory = slash >= 0 ? path.slice(0, slash) : '';
  const file = slash >= 0 ? path.slice(slash + 1) : path;
  return `${directory}/_rels/${file}.rels`;
}

async function readRelationships(path: string, readXml: (path: string) => Promise<XmlNode | undefined>): Promise<Map<string, Relationship>> {
  const xml = await readXml(path);
  const relationships = new Map<string, Relationship>();
  direct<XmlNode[]>(child(xml, 'Relationship', true)).forEach(item => {
    const id = attr(item, 'Id');
    const target = attr(item, 'Target');
    const type = attr(item, 'Type');
    if (!id || !target || !type) return;
    relationships.set(id, { id, target, type, external: attr(item, 'TargetMode') === 'External' });
  });
  return relationships;
}

function resolveTarget(sourcePath: string, target: string): string {
  const base = sourcePath.slice(0, sourcePath.lastIndexOf('/') + 1);
  const parts = `${base}${target}`.split('/');
  const normalized: string[] = [];
  for (const part of parts) {
    if (!part || part === '.') continue;
    if (part === '..') {
      if (normalized.length === 0) throw new Error('The presentation relationship points outside its package');
      normalized.pop();
    } else {
      normalized.push(part);
    }
  }
  return normalized.join('/');
}

function findRelationship(rels: Map<string, Relationship>, type: string): Relationship | undefined {
  return [...rels.values()].find(item => item.type.toLowerCase().includes(`/relationships/${type.toLowerCase()}`));
}

function readSlideSize(xml: XmlNode): { width: number; height: number } {
  const size = direct<XmlNode | undefined>(child(xml, 'sldSz'));
  return {
    width: emu(attrNumber(size, 'cx') ?? 9_144_000),
    height: emu(attrNumber(size, 'cy') ?? 6_858_000),
  };
}

function emptyTheme(): Theme {
  return {
    colors: new Map(),
    majorFont: { supplemental: new Map() },
    minorFont: { supplemental: new Map() },
  };
}

function parseTheme(xml: XmlNode): Theme {
  const theme = emptyTheme();
  const scheme = descendants(xml, 'clrScheme')[0];
  scheme?.children.forEach(item => {
    const color = resolveColor(item, theme);
    if (color) theme.colors.set(localName(item.name), color);
  });
  const major = descendants(xml, 'majorFont')[0];
  const minor = descendants(xml, 'minorFont')[0];
  theme.majorFont = parseThemeFont(major);
  theme.minorFont = parseThemeFont(minor);
  return theme;
}

function parseThemeFont(node: XmlNode | undefined): ThemeFont {
  const supplemental = new Map<string, string>();
  direct<XmlNode[]>(child(node, 'font', true)).forEach(font => {
    const script = attr(font, 'script');
    const typeface = attr(font, 'typeface');
    if (script && typeface) supplemental.set(script, typeface);
  });
  return {
    latin: nonEmpty(attr(direct<XmlNode | undefined>(child(node, 'latin')), 'typeface')),
    eastAsian: nonEmpty(attr(direct<XmlNode | undefined>(child(node, 'ea')), 'typeface')),
    complexScript: nonEmpty(attr(direct<XmlNode | undefined>(child(node, 'cs')), 'typeface')),
    supplemental,
  };
}

function emptyTemplate(): Template {
  return { background: EMPTY_FILL, nodes: [], placeholders: new Map() };
}

async function parseTemplate(
  part: Part,
  theme: Theme,
  readBlob: (path: string) => Promise<Blob | undefined>,
  readXml: (path: string) => Promise<XmlNode | undefined>,
): Promise<Template> {
  const tree = descendants(part.xml, 'spTree')[0];
  if (!tree) return emptyTemplate();
  const nodes = await parseNodes(tree.children, part, theme, ROOT_TRANSFORM, readBlob, readXml);
  const placeholders = new Map<string, PresentationShape>();
  nodes.forEach(node => {
    if (node.type === 'shape' && node.placeholderKey) placeholders.set(node.placeholderKey, node);
  });
  return { background: parseBackground(part.xml, theme), nodes, placeholders };
}

async function parseSlide(
  part: Part,
  index: number,
  theme: Theme,
  master: Template,
  layout: Template,
  readBlob: (path: string) => Promise<Blob | undefined>,
  readXml: (path: string) => Promise<XmlNode | undefined>,
): Promise<PresentationSlide> {
  const tree = descendants(part.xml, 'spTree')[0];
  const rawNodes = tree ? await parseNodes(tree.children, part, theme, ROOT_TRANSFORM, readBlob, readXml) : [];
  const nodes = rawNodes.map(node =>
    inheritPlaceholder(node, layout.placeholders.get(node.placeholderKey ?? '') ?? master.placeholders.get(node.placeholderKey ?? '')),
  );
  const templateNodes = [...master.nodes, ...layout.nodes].filter(node => !node.placeholderKey);
  const allNodes = [...templateNodes, ...nodes];
  const hasUnsupportedNodes = allNodes.some(node => node.type === 'unsupported');
  return {
    id: `slide-${index + 1}`,
    name: attr(descendants(part.xml, 'cSld')[0], 'name') || `Slide ${index + 1}`,
    background: firstFill(parseBackground(part.xml, theme), layout.background, master.background, WHITE_FILL),
    nodes: allNodes.filter(node => node.type !== 'unsupported'),
    status: hasUnsupportedNodes ? 'incomplete' : undefined,
  };
}

function inheritPlaceholder(node: PresentationNode, template: PresentationShape | undefined): PresentationNode {
  if (!template || node.type !== 'shape') return node;
  const hasBounds = node.width > 0 && node.height > 0;
  return {
    ...node,
    x: hasBounds ? node.x : template.x,
    y: hasBounds ? node.y : template.y,
    width: hasBounds ? node.width : template.width,
    height: hasBounds ? node.height : template.height,
    fill: node.fill.kind === 'none' ? template.fill : node.fill,
    stroke: node.stroke ?? template.stroke,
    text: node.text ? { ...template.text, ...node.text, margin: node.text.margin } : template.text,
  };
}

function firstFill(...fills: PresentationFill[]): PresentationFill {
  return fills.find(fill => fill.kind !== 'none') ?? WHITE_FILL;
}

async function parseNodes(
  children: XmlNode[],
  part: Part,
  theme: Theme,
  transform: Transform,
  readBlob: (path: string) => Promise<Blob | undefined>,
  readXml: (path: string) => Promise<XmlNode | undefined>,
  nodePath = 'root',
): Promise<PresentationNode[]> {
  const nodes: PresentationNode[] = [];
  for (let index = 0; index < children.length; index += 1) {
    const node = children[index];
    const sourcePath = `${nodePath}.${index}`;
    const type = localName(node.name);
    if (type === 'nvGrpSpPr' || type === 'grpSpPr') continue;
    if (type === 'sp') {
      const shape = parseShape(node, theme, transform);
      nodes.push(withSourceId(isSupportedGeometry(shape.geometry) ? shape : unsupported(node, transform, `shape: ${shape.geometry}`), part.path, sourcePath));
    } else if (type === 'pic') {
      const image = await parseImage(node, part, transform, readBlob);
      nodes.push(withSourceId(image ?? unsupported(node, transform, 'image'), part.path, sourcePath));
    } else if (type === 'graphicFrame') {
      nodes.push(
        withSourceId((await parseGraphicFrame(node, part, theme, transform, readXml)) ?? unsupported(node, transform, 'graphic object'), part.path, sourcePath),
      );
    } else if (type === 'grpSp') {
      const groupTransform = composeTransform(node, transform);
      nodes.push(...(await parseNodes(node.children, part, theme, groupTransform, readBlob, readXml, sourcePath)));
    } else if (type === 'cxnSp') {
      const connector = parseShape(node, theme, transform);
      nodes.push(
        withSourceId(
          isSupportedGeometry(connector.geometry) ? connector : unsupported(node, transform, `connector: ${connector.geometry}`),
          part.path,
          sourcePath,
        ),
      );
    } else if (['contentPart', 'graphic'].includes(type)) {
      nodes.push(withSourceId(unsupported(node, transform, 'graphic object'), part.path, sourcePath));
    }
  }
  return nodes;
}

function isSupportedGeometry(geometry: string): boolean {
  return SUPPORTED_GEOMETRIES.has(geometry) || geometry.includes('Connector');
}

function withSourceId<T extends PresentationNode>(node: T, path: string, nodePath: string): T {
  return { ...node, id: `${path}#${nodePath}:${node.id}` };
}

function parseShape(node: XmlNode, theme: Theme, transform: Transform): PresentationShape {
  const properties = direct<XmlNode | undefined>(child(node, 'spPr'));
  const textBody = direct<XmlNode | undefined>(child(node, 'txBody'));
  const rawBounds = readBounds(properties, transform);
  const nameNode = descendants(node, 'cNvPr')[0];
  const placeholder = descendants(node, 'ph')[0];
  const presetGeometry = direct<XmlNode | undefined>(child(properties, 'prstGeom'));
  const parsedGeometry = attr(presetGeometry, 'prst') ?? (child(properties, 'custGeom') ? 'custom' : 'rect');
  const stroke = parseStroke(properties, theme);
  const geometry = isLinearGeometry(parsedGeometry) && rawBounds.width === 0 && rawBounds.height > 0 ? 'lineVertical' : parsedGeometry;
  const bounds = normalizeLineBounds(rawBounds, geometry, stroke);
  return {
    type: 'shape',
    id: nodeId(nameNode, `shape-${bounds.x}-${bounds.y}-${bounds.width}-${bounds.height}`),
    name: attr(nameNode, 'name') ?? 'Shape',
    placeholderKey: placeholderKey(placeholder),
    geometry,
    adjustments: parseGeometryAdjustments(presetGeometry),
    fill: parseFill(properties, theme),
    stroke,
    text: textBody ? parseText(textBody, theme) : undefined,
    ...bounds,
  };
}

function parseGeometryAdjustments(geometry: XmlNode | undefined): Record<string, number> | undefined {
  const values: Record<string, number> = {};
  const adjustmentList = direct<XmlNode | undefined>(child(geometry, 'avLst'));
  direct<XmlNode[]>(child(adjustmentList, 'gd', true)).forEach(guide => {
    const name = attr(guide, 'name');
    const formula = attr(guide, 'fmla')?.trim();
    const match = formula?.match(/^val\s+(-?\d+(?:\.\d+)?)$/);
    if (name && match) values[name] = Number(match[1]);
  });
  return Object.keys(values).length > 0 ? values : undefined;
}

function isLinearGeometry(geometry: string): boolean {
  return geometry === 'line' || geometry.includes('Connector');
}

function normalizeLineBounds(bounds: PresentationBounds, geometry: string, stroke: PresentationStroke | undefined): PresentationBounds {
  if (!isLinearGeometry(geometry) && geometry !== 'lineVertical') return bounds;
  const thickness = Math.max(1, stroke?.width ?? 1);
  const width = bounds.width || thickness;
  const height = bounds.height || thickness;
  return {
    ...bounds,
    x: bounds.width === 0 ? bounds.x - thickness / 2 : bounds.x,
    y: bounds.height === 0 ? bounds.y - thickness / 2 : bounds.y,
    width,
    height,
  };
}

async function parseImage(
  node: XmlNode,
  part: Part,
  transform: Transform,
  readBlob: (path: string) => Promise<Blob | undefined>,
): Promise<PresentationImage | undefined> {
  const properties = direct<XmlNode | undefined>(child(node, 'spPr'));
  const blip = descendants(node, 'blip')[0];
  const relationship = part.rels.get(attr(blip, 'r:embed') ?? '');
  if (!relationship || relationship.external) return undefined;
  const image = await readBlob(resolveTarget(part.path, relationship.target));
  if (!image) return undefined;
  const nameNode = descendants(node, 'cNvPr')[0];
  const crop = descendants(node, 'srcRect')[0];
  const bounds = readBounds(properties, transform);
  return {
    type: 'image',
    id: nodeId(nameNode, `image-${bounds.x}-${bounds.y}-${bounds.width}-${bounds.height}`),
    name: attr(nameNode, 'name') ?? 'Image',
    alt: attr(nameNode, 'descr') ?? attr(nameNode, 'name') ?? 'Image',
    image,
    crop: crop
      ? {
          left: (attrNumber(crop, 'l') ?? 0) / 100_000,
          right: (attrNumber(crop, 'r') ?? 0) / 100_000,
          top: (attrNumber(crop, 't') ?? 0) / 100_000,
          bottom: (attrNumber(crop, 'b') ?? 0) / 100_000,
        }
      : undefined,
    ...bounds,
  };
}

async function parseGraphicFrame(
  node: XmlNode,
  part: Part,
  theme: Theme,
  transform: Transform,
  readXml: (path: string) => Promise<XmlNode | undefined>,
): Promise<PresentationTable | PresentationChart | undefined> {
  const graphicData = descendants(node, 'graphicData')[0];
  if (!graphicData) return undefined;
  const bounds = readBounds(node, transform);
  const nameNode = descendants(node, 'cNvPr')[0];
  const name = attr(nameNode, 'name') ?? 'Graphic';
  const table = descendants(graphicData, 'tbl')[0];
  if (table) return parseTable(table, { id: nodeId(nameNode, `table-${bounds.x}-${bounds.y}-${bounds.width}-${bounds.height}`), name, ...bounds }, theme);
  const chart = descendants(graphicData, 'chart')[0];
  const relationship = part.rels.get(attr(chart, 'r:id') ?? '');
  if (!chart || !relationship || relationship.external) return undefined;
  const chartXml = await readXml(resolveTarget(part.path, relationship.target));
  return chartXml
    ? parseChart(chartXml, { id: nodeId(nameNode, `chart-${bounds.x}-${bounds.y}-${bounds.width}-${bounds.height}`), name, ...bounds }, theme)
    : undefined;
}

function parseTable(table: XmlNode, base: Omit<PresentationTable, 'type' | 'columns' | 'rows'>, theme: Theme): PresentationTable {
  const properties = direct<XmlNode | undefined>(child(table, 'tblPr'));
  const styleId = textContent(direct<XmlNode | undefined>(child(properties, 'tableStyleId'))).trim();
  const style = builtInTableStyle(styleId, theme);
  const hasFirstRow = attr(properties, 'firstRow') === '1' || attr(properties, 'firstRow') === 'true';
  const hasRowBands = attr(properties, 'bandRow') === '1' || attr(properties, 'bandRow') === 'true';
  const grid = direct<XmlNode[]>(child(direct<XmlNode | undefined>(child(table, 'tblGrid')), 'gridCol', true));
  const rows = direct<XmlNode[]>(child(table, 'tr', true)).map((row, rowIndex) => ({
    height: emu(attrNumber(row, 'h') ?? 0),
    cells: direct<XmlNode[]>(child(row, 'tc', true)).map(cell => {
      const cellProperties = direct<XmlNode | undefined>(child(cell, 'tcPr'));
      return {
        text: parseTableText(cell, theme),
        fill: hasCellFill(cellProperties) ? parseFill(cellProperties, theme) : resolveTableStyleFill(style, rowIndex, hasFirstRow, hasRowBands),
        stroke: parseStroke(cellProperties, theme),
        colSpan: attrNumber(cellProperties, 'gridSpan') ?? 1,
        rowSpan: attrNumber(cellProperties, 'rowSpan') ?? 1,
        merged: attr(cellProperties, 'hMerge') === '1' || attr(cellProperties, 'vMerge') === '1',
      };
    }),
  }));
  return { type: 'table', ...base, columns: grid.map(item => emu(attrNumber(item, 'w') ?? 0)), rows };
}

function hasCellFill(properties: XmlNode | undefined): boolean {
  return Boolean(child(properties, 'solidFill') || child(properties, 'gradFill') || child(properties, 'noFill'));
}

function resolveTableStyleFill(style: TableStyle | undefined, rowIndex: number, hasFirstRow: boolean, hasRowBands: boolean): PresentationFill {
  if (!style) return EMPTY_FILL;
  if (hasFirstRow && rowIndex === 0 && style.firstRow) return style.firstRow;
  const dataRowIndex = rowIndex - (hasFirstRow ? 1 : 0);
  if (hasRowBands && dataRowIndex >= 0 && dataRowIndex % 2 === 0 && style.band1H) return style.band1H;
  return style.wholeTbl ?? EMPTY_FILL;
}

function builtInTableStyle(styleId: string, theme: Theme): TableStyle | undefined {
  if (styleId !== MEDIUM_STYLE_2_ACCENT_1) return undefined;
  const accent = theme.colors.get('accent1');
  if (!accent) return undefined;
  return {
    wholeTbl: solidTint(accent, 0.2),
    band1H: solidTint(accent, 0.4),
    firstRow: { kind: 'solid', color: accent },
  };
}

function solidTint(color: string, retainedFraction: number): PresentationFill {
  const rgb = parseHex(color);
  if (!rgb) return EMPTY_FILL;
  const retained = Math.min(1, Math.max(0, retainedFraction));
  const tinted = rgb.map(channel => {
    const linear = srgbChannelToLinear(channel / 255);
    return Math.round(linearChannelToSrgb(linear * retained + (1 - retained)) * 255);
  });
  return { kind: 'solid', color: `#${tinted.map(item => item.toString(16).padStart(2, '0')).join('')}`.toUpperCase() };
}

function srgbChannelToLinear(channel: number): number {
  return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
}

function linearChannelToSrgb(channel: number): number {
  return channel <= 0.0031308 ? channel * 12.92 : 1.055 * channel ** (1 / 2.4) - 0.055;
}

function parseTableText(cell: XmlNode, theme: Theme): PresentationText | undefined {
  const textBody = direct<XmlNode | undefined>(child(cell, 'txBody'));
  return textBody ? parseText(textBody, theme) : undefined;
}

function parseChart(xml: XmlNode, base: Omit<PresentationChart, 'type' | 'chartType' | 'title' | 'series'>, theme: Theme): PresentationChart | undefined {
  const plot = descendants(xml, 'plotArea')[0];
  if (!plot) return undefined;
  const typeNode = ['barChart', 'lineChart', 'pieChart'].map(kind => [kind, direct<XmlNode | undefined>(child(plot, kind))] as const).find(([, item]) => item);
  if (!typeNode || !typeNode[1]) return undefined;
  const [kind, node] = typeNode;
  const chartType: PresentationChart['chartType'] =
    kind === 'barChart'
      ? attr(direct<XmlNode | undefined>(child(node, 'barDir')), 'val') === 'bar'
        ? 'bar'
        : 'column'
      : kind === 'lineChart'
        ? 'line'
        : 'pie';
  const series = direct<XmlNode[]>(child(node, 'ser', true)).map((seriesNode, index) => parseChartSeries(seriesNode, theme, index));
  if (series.length === 0) return undefined;
  const title = descendants(xml, 'title')[0];
  return { type: 'chart', ...base, chartType, title: title ? textContent(title).trim() || undefined : undefined, series };
}

function parseChartSeries(node: XmlNode, theme: Theme, index: number): PresentationChart['series'][number] {
  const name = textContent(descendants(node, 'tx')[0]).trim() || undefined;
  const categories = descendants(descendants(node, 'cat')[0], 'pt').map(point => textContent(point).trim());
  const values = descendants(descendants(node, 'val')[0], 'pt')
    .map(point => Number(textContent(point)))
    .filter(Number.isFinite);
  const color = resolveColor(descendants(node, 'solidFill')[0], theme) ?? theme.colors.get(`accent${(index % 6) + 1}`);
  return { name, categories, values, color };
}

function unsupported(node: XmlNode, transform: Transform, feature: string): PresentationNode {
  const properties = direct<XmlNode | undefined>(child(node, 'spPr'));
  const nameNode = descendants(node, 'cNvPr')[0];
  return {
    type: 'unsupported',
    id: nodeId(nameNode, `unsupported-${feature}-${attr(nameNode, 'name') ?? 'object'}`),
    name: attr(nameNode, 'name') ?? feature,
    feature,
    ...readBounds(properties ?? node, transform),
  };
}

function placeholderKey(node: XmlNode | undefined): string | undefined {
  if (!node) return undefined;
  const type = attr(node, 'type') ?? 'body';
  const index = attr(node, 'idx') ?? '0';
  return `${type}:${index}`;
}

function nodeId(node: XmlNode | undefined, fallback: string): string {
  return attr(node, 'id') ?? fallback;
}

function composeTransform(node: XmlNode, parent: Transform): Transform {
  const properties = direct<XmlNode | undefined>(child(node, 'grpSpPr'));
  const xfrm = direct<XmlNode | undefined>(child(properties, 'xfrm'));
  const off = direct<XmlNode | undefined>(child(xfrm, 'off'));
  const ext = direct<XmlNode | undefined>(child(xfrm, 'ext'));
  const childOff = direct<XmlNode | undefined>(child(xfrm, 'chOff'));
  const childExt = direct<XmlNode | undefined>(child(xfrm, 'chExt'));
  const x = attrNumber(off, 'x') ?? 0;
  const y = attrNumber(off, 'y') ?? 0;
  const width = attrNumber(ext, 'cx') ?? 0;
  const height = attrNumber(ext, 'cy') ?? 0;
  const childX = attrNumber(childOff, 'x') ?? 0;
  const childY = attrNumber(childOff, 'y') ?? 0;
  const childWidth = (attrNumber(childExt, 'cx') ?? width) || 1;
  const childHeight = (attrNumber(childExt, 'cy') ?? height) || 1;
  const scaleX = parent.scaleX * (width / childWidth || 1);
  const scaleY = parent.scaleY * (height / childHeight || 1);
  return {
    scaleX,
    scaleY,
    offsetX: parent.offsetX + parent.scaleX * (x - childX * (width / childWidth || 1)),
    offsetY: parent.offsetY + parent.scaleY * (y - childY * (height / childHeight || 1)),
  };
}

function readBounds(properties: XmlNode | undefined, transform: Transform): PresentationBounds {
  const xfrm = direct<XmlNode | undefined>(child(properties, 'xfrm'));
  const off = direct<XmlNode | undefined>(child(xfrm, 'off'));
  const ext = direct<XmlNode | undefined>(child(xfrm, 'ext'));
  const x = attrNumber(off, 'x') ?? 0;
  const y = attrNumber(off, 'y') ?? 0;
  const width = attrNumber(ext, 'cx') ?? 0;
  const height = attrNumber(ext, 'cy') ?? 0;
  return {
    x: emu(transform.offsetX + x * transform.scaleX),
    y: emu(transform.offsetY + y * transform.scaleY),
    width: emu(width * transform.scaleX),
    height: emu(height * transform.scaleY),
    rotation: (attrNumber(xfrm, 'rot') ?? 0) / 60_000,
    flipH: attr(xfrm, 'flipH') === '1' || attr(xfrm, 'flipH') === 'true',
    flipV: attr(xfrm, 'flipV') === '1' || attr(xfrm, 'flipV') === 'true',
  };
}

function parseBackground(xml: XmlNode, theme: Theme): PresentationFill {
  const background = descendants(xml, 'bg')[0];
  const properties = direct<XmlNode | undefined>(child(background, 'bgPr'));
  return parseFill(properties ?? background, theme);
}

function parseFill(node: XmlNode | undefined, theme: Theme): PresentationFill {
  if (!node) return EMPTY_FILL;
  const solid = direct<XmlNode | undefined>(child(node, 'solidFill'));
  if (solid) {
    const color = resolveColor(solid, theme);
    return color ? { kind: 'solid', color, transparency: resolveTransparency(solid) } : EMPTY_FILL;
  }
  const gradient = direct<XmlNode | undefined>(child(node, 'gradFill'));
  if (gradient) {
    const stops = descendants(gradient, 'gs').map(stop => {
      const color = resolveColor(stop, theme) ?? '#000000';
      return { offset: (attrNumber(stop, 'pos') ?? 0) / 100_000, color, transparency: resolveTransparency(stop) };
    });
    if (stops.length > 0) {
      const linear = direct<XmlNode | undefined>(child(gradient, 'lin'));
      return { kind: 'gradient', angle: (attrNumber(linear, 'ang') ?? 0) / 60_000, stops };
    }
  }
  return EMPTY_FILL;
}

function parseStroke(node: XmlNode | undefined, theme: Theme): PresentationStroke | undefined {
  const line = direct<XmlNode | undefined>(child(node, 'ln'));
  if (!line || child(line, 'noFill')) return undefined;
  const color = resolveColor(line, theme);
  if (!color) return undefined;
  const dash = attr(direct<XmlNode | undefined>(child(line, 'prstDash')), 'val');
  return {
    color,
    width: Math.max(0.5, emu(attrNumber(line, 'w') ?? 12_700)),
    dash: dash === 'dash' ? '6 4' : dash === 'dot' ? '1 3' : dash === 'lgDash' ? '10 5' : undefined,
  };
}

function resolveColor(node: XmlNode | undefined, theme: Theme): PresentationColor | undefined {
  if (!node) return undefined;
  const colorNode = findColorNode(node);
  if (!colorNode) return undefined;
  let color: string | undefined;
  switch (localName(colorNode.name)) {
    case 'srgbClr':
      color = attr(colorNode, 'val');
      break;
    case 'schemeClr':
      color = theme.colors.get(attr(colorNode, 'val') ?? '');
      break;
    case 'sysClr':
      color = attr(colorNode, 'lastClr') ?? attr(colorNode, 'val');
      break;
    case 'prstClr':
      color = presetColor(attr(colorNode, 'val'));
      break;
  }
  if (!color) return undefined;
  const initialRgb = parseHex(color);
  if (!initialRgb) return undefined;
  let rgb: [number, number, number] = initialRgb;
  colorNode.children.forEach(modifier => {
    const value = (attrNumber(modifier, 'val') ?? 0) / 100_000;
    if (localName(modifier.name) === 'tint') rgb = rgb.map(item => item + (255 - item) * value) as [number, number, number];
    if (localName(modifier.name) === 'shade') rgb = rgb.map(item => item * value) as [number, number, number];
    if (localName(modifier.name) === 'lumMod') rgb = rgb.map(item => item * value) as [number, number, number];
    if (localName(modifier.name) === 'lumOff') rgb = rgb.map(item => item + 255 * value) as [number, number, number];
  });
  return `#${rgb
    .map(item =>
      Math.round(Math.min(255, Math.max(0, item)))
        .toString(16)
        .padStart(2, '0'),
    )
    .join('')}`.toUpperCase();
}

function resolveTransparency(node: XmlNode): number | undefined {
  const color = findColorNode(node);
  const alpha = color?.children.find(item => localName(item.name) === 'alpha');
  return alpha ? 1 - (attrNumber(alpha, 'val') ?? 100_000) / 100_000 : undefined;
}

function findColorNode(node: XmlNode): XmlNode | undefined {
  const isColor = (item: XmlNode) => ['srgbClr', 'schemeClr', 'sysClr', 'prstClr'].includes(localName(item.name));
  return node.children.find(isColor) ?? direct<XmlNode | undefined>(child(node, 'solidFill'))?.children.find(isColor);
}

function parseText(node: XmlNode, theme: Theme): PresentationText {
  const bodyProperties = direct<XmlNode | undefined>(child(node, 'bodyPr'));
  const listStyle = direct<XmlNode | undefined>(child(node, 'lstStyle'));
  const defaultParagraph = direct<XmlNode | undefined>(child(listStyle, 'defPPr')) ?? direct<XmlNode | undefined>(child(listStyle, 'lvl1pPr'));
  const defaultRun = direct<XmlNode | undefined>(child(defaultParagraph, 'defRPr'));
  const normalAutofit = direct<XmlNode | undefined>(child(bodyProperties, 'normAutofit'));
  const shapeAutofit = direct<XmlNode | undefined>(child(bodyProperties, 'spAutoFit'));
  const noAutofit = direct<XmlNode | undefined>(child(bodyProperties, 'noAutofit'));
  const fontScale = parsePercentage(attr(normalAutofit, 'fontScale')) ?? 1;
  const lineSpacingReduction = parsePercentage(attr(normalAutofit, 'lnSpcReduction')) ?? 0;
  const paragraphs = direct<XmlNode[]>(child(node, 'p', true)).map(paragraph => parseParagraph(paragraph, listStyle, theme, fontScale, lineSpacingReduction));
  const vertical = attr(bodyProperties, 'vert');
  const anchor = attr(bodyProperties, 'anchor');
  const fonts = parseFontFamilies([defaultRun], theme, '');
  return {
    paragraphs,
    margin: {
      left: emu(attrNumber(bodyProperties, 'lIns') ?? 91_440),
      right: emu(attrNumber(bodyProperties, 'rIns') ?? 91_440),
      top: emu(attrNumber(bodyProperties, 'tIns') ?? 45_720),
      bottom: emu(attrNumber(bodyProperties, 'bIns') ?? 45_720),
    },
    vertical: vertical === 'vert' || vertical === 'vert270' || vertical === 'eaVert',
    verticalReverse: vertical === 'vert270',
    anchor: anchor === 'ctr' || anchor === 'b' ? (anchor === 'ctr' ? 'middle' : 'bottom') : 'top',
    autoFit: normalAutofit ? 'shrink' : shapeAutofit ? 'resize' : noAutofit ? 'none' : undefined,
    ...fonts,
    fontSize: parseFontSize([defaultRun], fontScale),
    color: resolveColor(defaultRun, theme),
  };
}

function parseParagraph(node: XmlNode, listStyle: XmlNode | undefined, theme: Theme, fontScale: number, lineSpacingReduction: number): PresentationParagraph {
  const properties = direct<XmlNode | undefined>(child(node, 'pPr'));
  const level = attrNumber(properties, 'lvl') ?? 0;
  const listProperties =
    direct<XmlNode | undefined>(child(listStyle, `lvl${Math.min(9, Math.max(1, level + 1))}pPr`)) ?? direct<XmlNode | undefined>(child(listStyle, 'defPPr'));
  const paragraphDefaultRun = direct<XmlNode | undefined>(child(properties, 'defRPr'));
  const listDefaultRun = direct<XmlNode | undefined>(child(listProperties, 'defRPr'));
  const runs: PresentationRun[] = [];
  node.children.forEach(item => {
    const kind = localName(item.name);
    if (kind === 'br') {
      runs.push({
        text: '\n',
        ...parseRunStyle([direct<XmlNode | undefined>(child(item, 'rPr')), paragraphDefaultRun, listDefaultRun], theme, '\n', fontScale),
      });
      return;
    }
    if (kind !== 'r' && kind !== 'fld') return;
    const runProperties = direct<XmlNode | undefined>(child(item, 'rPr'));
    const text = textContent(direct<XmlNode | undefined>(child(item, 't')));
    runs.push({
      text,
      ...parseRunStyle([runProperties, paragraphDefaultRun, listDefaultRun], theme, text, fontScale),
    });
  });
  const alignment = firstAttribute([properties, listProperties], 'algn');
  const marginLeft = firstNumber([properties, listProperties], 'marL');
  const indent = firstNumber([properties, listProperties], 'indent');
  return {
    runs,
    align: alignment === 'ctr' ? 'center' : alignment === 'r' ? 'right' : alignment === 'just' ? 'justify' : 'left',
    level,
    bullet: parseBullet(properties, listProperties),
    marginLeft: marginLeft === undefined ? undefined : emu(marginLeft),
    indent: indent === undefined ? undefined : emu(indent),
    spaceBefore: parseSpacing(firstChild([properties, listProperties], 'spcBef')),
    spaceAfter: parseSpacing(firstChild([properties, listProperties], 'spcAft')),
    lineSpacing: parseSpacing(firstChild([properties, listProperties], 'lnSpc'), lineSpacingReduction),
  };
}

function parseBullet(primary: XmlNode | undefined, fallback: XmlNode | undefined): PresentationParagraph['bullet'] {
  for (const properties of [primary, fallback]) {
    if (!properties) continue;
    if (child(properties, 'buNone')) return undefined;
    const character = direct<XmlNode | undefined>(child(properties, 'buChar'));
    if (character) return { kind: 'character', value: attr(character, 'char') ?? '•' };
    if (child(properties, 'buAutoNum')) return { kind: 'number' };
  }
  return undefined;
}

function parseRunStyle(nodes: Array<XmlNode | undefined>, theme: Theme, text: string, fontScale: number): Omit<PresentationRun, 'text'> {
  const fonts = parseFontFamilies(nodes, theme, text);
  const spacing = firstNumber(nodes, 'spc');
  const kerning = firstNumber(nodes, 'kern');
  return {
    ...fonts,
    fontSize: parseFontSize(nodes, fontScale),
    characterSpacing: spacing === undefined ? undefined : pointHundredths(spacing),
    kerningThreshold: kerning === undefined ? undefined : pointHundredths(kerning),
    color: firstResolvedColor(nodes, theme),
    bold: firstBooleanAttribute(nodes, 'b'),
    italic: firstBooleanAttribute(nodes, 'i'),
    underline: firstUnderline(nodes),
    baseline: firstNumber(nodes, 'baseline'),
    hyperlink: firstChildAttribute(nodes, 'hlinkClick', 'r:id'),
  };
}

function firstResolvedColor(nodes: Array<XmlNode | undefined>, theme: Theme): PresentationColor | undefined {
  for (const node of nodes) {
    const color = resolveColor(node, theme);
    if (color !== undefined) return color;
  }
  return undefined;
}

function firstBooleanAttribute(nodes: Array<XmlNode | undefined>, name: string): boolean | undefined {
  const value = firstAttribute(nodes, name);
  return value === undefined ? undefined : value === '1' || value === 'true';
}

function firstUnderline(nodes: Array<XmlNode | undefined>): boolean | undefined {
  const value = firstAttribute(nodes, 'u');
  return value === undefined ? undefined : value !== 'none';
}

function parseSpacing(node: XmlNode | undefined, reduction = 0): PresentationSpacing | undefined {
  if (!node) return undefined;
  const points = direct<XmlNode | undefined>(child(node, 'spcPts'));
  if (points) return { kind: 'absolute', value: pointHundredths(attrNumber(points, 'val') ?? 0) };
  const percentage = direct<XmlNode | undefined>(child(node, 'spcPct'));
  return percentage ? { kind: 'relative', value: ((attrNumber(percentage, 'val') ?? 0) / 100_000) * Math.max(0, 1 - reduction) } : undefined;
}

function parseFontSize(nodes: Array<XmlNode | undefined>, fontScale: number): number | undefined {
  const value = firstNumber(nodes, 'sz');
  return value === undefined ? undefined : pointHundredths(value) * fontScale;
}

function parseFontFamilies(
  nodes: Array<XmlNode | undefined>,
  theme: Theme,
  text: string,
): Pick<PresentationRun, 'fontFamily' | 'eastAsianFontFamily' | 'complexScriptFontFamily'> {
  const language = firstAttribute(nodes, 'lang') ?? firstAttribute(nodes, 'altLang');
  return {
    fontFamily: resolveTypeface(firstChildAttribute(nodes, 'latin', 'typeface'), 'latin', theme, language, text),
    eastAsianFontFamily: resolveTypeface(firstChildAttribute(nodes, 'ea', 'typeface'), 'eastAsian', theme, language, text),
    complexScriptFontFamily: resolveTypeface(firstChildAttribute(nodes, 'cs', 'typeface'), 'complexScript', theme, language, text),
  };
}

function resolveTypeface(
  value: string | undefined,
  script: 'latin' | 'eastAsian' | 'complexScript',
  theme: Theme,
  language: string | undefined,
  text: string,
): string | undefined {
  const typeface = nonEmpty(value);
  const token = typeface?.match(/^\+(mj|mn)-(lt|ea|cs)$/);
  if (token) {
    const font = token[1] === 'mj' ? theme.majorFont : theme.minorFont;
    const tokenScript = token[2] === 'lt' ? 'latin' : token[2] === 'ea' ? 'eastAsian' : 'complexScript';
    return themeTypeface(font, tokenScript, language, text);
  }
  return typeface ?? themeTypeface(theme.minorFont, script, language, text);
}

function themeTypeface(font: ThemeFont, script: 'latin' | 'eastAsian' | 'complexScript', language: string | undefined, text: string): string | undefined {
  if (script === 'latin') return font.latin;
  if (script === 'complexScript') return font.complexScript;
  if (font.eastAsian) return font.eastAsian;
  const supplementalScript = eastAsianThemeScript(language, text);
  return supplementalScript ? font.supplemental.get(supplementalScript) : undefined;
}

function eastAsianThemeScript(language: string | undefined, text: string): string | undefined {
  const normalized = language?.toLowerCase();
  if (normalized?.startsWith('ja')) return 'Jpan';
  if (normalized?.startsWith('ko')) return 'Hang';
  if (normalized && /^zh-(tw|hk|mo)\b/.test(normalized)) return 'Hant';
  if (normalized?.startsWith('zh')) return 'Hans';
  if (/[\u3040-\u30ff]/u.test(text)) return 'Jpan';
  if (/[\uac00-\ud7af]/u.test(text)) return 'Hang';
  if (/[\u3100-\u312f\u31a0-\u31bf]/u.test(text)) return 'Hant';
  if (/[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/u.test(text)) return 'Hans';
  return undefined;
}

function firstAttribute(nodes: Array<XmlNode | undefined>, name: string): string | undefined {
  for (const node of nodes) {
    const value = attr(node, name);
    if (value !== undefined) return value;
  }
  return undefined;
}

function firstNumber(nodes: Array<XmlNode | undefined>, name: string): number | undefined {
  for (const node of nodes) {
    const value = attrNumber(node, name);
    if (value !== undefined) return value;
  }
  return undefined;
}

function firstChild(nodes: Array<XmlNode | undefined>, name: string): XmlNode | undefined {
  for (const node of nodes) {
    const value = direct<XmlNode | undefined>(child(node, name));
    if (value) return value;
  }
  return undefined;
}

function firstChildAttribute(nodes: Array<XmlNode | undefined>, childName: string, attributeName: string): string | undefined {
  for (const node of nodes) {
    const value = attr(direct<XmlNode | undefined>(child(node, childName)), attributeName);
    if (value !== undefined) return value;
  }
  return undefined;
}

function parsePercentage(value: string | undefined): number | undefined {
  if (!value) return undefined;
  const normalized = value.trim();
  const number = Number(normalized.endsWith('%') ? normalized.slice(0, -1) : normalized);
  if (!Number.isFinite(number)) return undefined;
  return normalized.endsWith('%') ? number / 100 : number / 100_000;
}

function pointHundredths(value: number): number {
  return (value / 100) * CSS_PIXELS_PER_POINT;
}

function nonEmpty(value: string | undefined): string | undefined {
  const normalized = value?.trim();
  return normalized ? normalized : undefined;
}

function attrNumber(node: XmlNode | undefined, name: string): number | undefined {
  const value = attr(node, name);
  if (value === undefined || value === '') return undefined;
  const number = Number(value);
  return Number.isFinite(number) ? number : undefined;
}

function emu(value: number): number {
  return value / EMUS_PER_PIXEL;
}

function textContent(node: XmlNode | undefined): string {
  if (!node) return '';
  return node.text + node.children.map(textContent).join('');
}

function parseHex(value: string): [number, number, number] | undefined {
  const normalized = value.replace('#', '');
  if (!/^[0-9a-f]{6}$/i.test(normalized)) return undefined;
  return [Number.parseInt(normalized.slice(0, 2), 16), Number.parseInt(normalized.slice(2, 4), 16), Number.parseInt(normalized.slice(4, 6), 16)];
}

function presetColor(value: string | undefined): string | undefined {
  const colors: Record<string, string> = {
    black: '000000',
    white: 'FFFFFF',
    red: 'FF0000',
    green: '008000',
    blue: '0000FF',
    yellow: 'FFFF00',
    gray: '808080',
    grey: '808080',
    dkGray: '404040',
    ltGray: 'C0C0C0',
    orange: 'FFA500',
    purple: '800080',
    brown: 'A52A2A',
    transparent: '000000',
  };
  return value ? colors[value] : undefined;
}

function mediaMimeType(path: string): string {
  const extension = path.slice(path.lastIndexOf('.') + 1).toLowerCase();
  return (
    (
      { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', bmp: 'image/bmp', webp: 'image/webp', svg: 'image/svg+xml' } as Record<
        string,
        string
      >
    )[extension] ?? 'application/octet-stream'
  );
}
