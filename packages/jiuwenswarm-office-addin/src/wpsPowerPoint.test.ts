import { strFromU8, strToU8, unzipSync, zipSync } from 'fflate';
import { describe, expect, it, vi } from 'vitest';

import type { ClientToolResult } from './clientAttachment';
import { ClientToolDispatcher, joinRuntimes } from './clientToolDispatcher';
import { FakeDeck, uploadedDeck } from './fakePowerPoint';
import { FakeWps } from './fakeWps';
import type { JsonObject } from './gatewayClient';
import { HostWriteQueue } from './hostWriteQueue';
import { nodeSandbox } from './nodeSandbox';
import { POWERPOINT } from './powerPointApp';
import { powerPointPackageRuntime } from './powerPointPackageTools';
import { powerPointReadRuntime } from './powerPointReadTools';
import { powerPointSlideDeck, powerPointWriteRuntime } from './powerPointWriteTools';
import { sha256Hex } from './presentationAssetClient';
import { SlideSnapshots } from './slideSnapshots';
import { WPS_HOST, wpsPowerPointRun } from './wpsPowerPoint';

const HELLO_SHA256 = '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824';
const SLIDE_RELATIONSHIP = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide';
const DESIGN ={ colors: { accent: '#18B7A0', text: '#0B1220' }, fonts: { heading: 'Arial', body: 'Arial' } };
const TEXT = (id: string, text: string, extra: JsonObject = {}) => ({ id, name: `Text ${id}`, type: 'TextBox', text,
  left: 40, top: 40, width: 400, height: 60, ...extra });

/** The pane's runtimes over the WPS adapter on a fake WPS, as the WPS task pane wires them. */
function pane() {
  const deck = new FakeDeck({ size: { width: 720, height: 405 }, slides: [
    { id: '256#', shapes: [TEXT('2', 'Atlasware grew', { fill: { color: '#112233' }, font: { color: '#112244' } })] },
    { id: '257#', shapes: [TEXT('2', 'Plan')] }] });
  const wps = new FakeWps(deck);
  const run = wpsPowerPointRun(wps.presentation, wps.application);
  const writes = new HostWriteQueue();
  const snapshots = new SlideSnapshots(powerPointSlideDeck(run), writes, { turns: 0, bytes: 0 });
  const fetcher = vi.fn(async (_url: string, _init: RequestInit) => new Response('hello'));
  const runtime = joinRuntimes(
    powerPointReadRuntime({ run, host: WPS_HOST, documentName: async () => 'Deck.pptx',
      languages: { displayLanguage: 'zh-CN', contentLanguage: 'zh-CN' } }),
    powerPointWriteRuntime({ run, host: WPS_HOST, journal: id => snapshots.journal(id), fetcher }),
    powerPointPackageRuntime({ run, host: WPS_HOST, journal: id => snapshots.journal(id), sandbox: nodeSandbox(),
      forgetUndo: () => snapshots.forget(), insertTakesDeckDesign: true, fetcher }));
  const dispatcher = new ClientToolDispatcher(POWERPOINT, runtime, writes);
  let calls = 0;
  const call = (toolName: string, input: JsonObject = {}, downloads: Record<string, string> = {}): Promise<ClientToolResult> => {
    calls += 1;
    return dispatcher.execute({ requestId: `c${calls}`, interruptId: 'i', toolName, input, downloads });
  };
  const answer = async (toolName: string, input: JsonObject = {}, downloads: Record<string, string> = {}) => {
    const [block] = (await call(toolName, input, downloads)).content;
    if (block.type !== 'text') throw new Error('no text block');
    return JSON.parse(block.text) as JsonObject;
  };
  /** ``import_slides``'s asset and downloads, the fetcher serving a deck of these slide texts. */
  const uploaded = async (texts: string[]) => {
    const bytes = uploadedDeck(texts);
    fetcher.mockImplementation(async () => new Response(bytes));
    const asset = { handle: 'oa_deck', purpose: 'input_pptx', size: bytes.length, sha256: await sha256Hex(bytes),
      mime_type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation' };
    return { asset, downloads: { oa_deck: '/file-api/download?token=t' } };
  };
  return { deck, wps, call, answer, uploaded };
}

describe('the PowerPoint runtimes on WPS Presentation', () => {
  it('read the deck: slides, page size, masters, shapes with geometry, text and colours, and the fingerprint', async () => {
    const { answer } = pane();
    const context = await answer('read_presentation_context');
    expect(context).toMatchObject({
      slide_count: 2, slide_width_points: 720, slide_height_points: 405, has_content: true, is_default_theme: true,
      masters: [{ id: 'm1', name: 'WPS', layouts: expect.arrayContaining([{ id: 'm1l7', name: 'Blank' }]) }],
      slides_metadata: [{ slide_id: '256', position: 1 }, { slide_id: '257', position: 2 }],
      structure_fingerprint: expect.stringMatching(/^ppt-[0-9a-f]{8}$/),
    });
    expect(context).not.toHaveProperty('dimensions_assumed_warning');
    expect((await answer('list_slide_shapes', { slide_id: '256' })).result).toEqual([{ ref: '256:2', id: '2',
      name: 'Text 2', type: 'TextBox', left: 40, top: 40, width: 400, height: 60, text: 'Atlasware grew' }]);
    expect((await answer('verify_slides', {})).result).toMatchObject({ slides: [
      { slideId: '256', contrast_warnings: [{ shape_id: '2', font_color: '#112244', bg_color: '#112233' }] },
      { slideId: '257' }] });
  });

  it('render a slide through Slide.Export at the slide\'s ratio and remove the PNG', async () => {
    const { call, wps } = pane();
    const result = await call('read_slide_image', { slide_id: '257', width: 800 });
    expect(result.content).toContainEqual({ type: 'image',
      source: { type: 'base64', media_type: 'image/png', data: btoa('PNG') } });
    expect(wps.exports).toEqual([{ width: 800, height: 450 }]);
    expect([...wps.files.keys()]).toEqual([]);
  });

  it('build a slide with a text box, a filled shape, a table and a picture from a downloaded file', async () => {
    const { answer, deck, wps } = pane();
    const base = (await answer('read_presentation_context')).structure_fingerprint;
    const asset = { key: 'logo', handle: 'oa_logo', purpose: 'input_image', mime_type: 'image/png', size: 5,
      sha256: HELLO_SHA256 };
    const result = await answer('presentation_apply_patch', { base_structure_fingerprint: base, design_system: DESIGN,
      slides: [
        { slide_ref: 'S1', slide_id: '256', operations: [{ op: 'update_shape', shape_id: '2', text: 'Grew' }] },
        { slide_ref: 'S3', operations: [
          { op: 'add_text', role: 'title', text: 'Title', left: 48, top: 36, width: 600, height: 54 },
          { op: 'add_shape', shape_type: 'Ellipse', fill: { color: 'accent' }, left: 48, top: 100, width: 100, height: 100 },
          { op: 'add_table', values: [['Item', 'Qty'], ['A', '1']], left: 48, top: 220, width: 400, height: 100 },
          { op: 'add_image', asset_key: 'logo', left: 500, top: 100, width: 100, height: 50 },
          // Written into the slide's package once the shapes above are drawn: the export must see them.
          { op: 'add_connector', left: 150, top: 150, width: 300, height: 0 },
        ] }], assets: [asset] }, { oa_logo: '/file-api/download?token=t' });
    expect(result).toMatchObject({ success: true, result: { created_slide_ids: [String(parseInt(deck.ids()[2], 10))] } });
    expect((result.result as JsonObject).shapes).toHaveLength(6);
    expect(deck.slides[0].shapes[0]).toMatchObject({ text: 'Grew' });
    const [title, ellipse, table, picture, connector] = deck.slides[2].shapes;
    expect(connector).toMatchObject({ type: 'Line' });
    expect(wps.saves).toBe(2);
    expect(title).toMatchObject({ type: 'TextBox', text: 'Title', left: 48, top: 36, font: { bold: -1, size: 32 } });
    expect(ellipse).toMatchObject({ type: 'GeometricShape', geometry: 9, fill: { color: '#18B7A0', visible: -1 } });
    expect(table).toMatchObject({ type: 'Table', rows: 2, columns: 2,
      cells: { '0,0': { text: 'Item', font: { bold: -1 } }, '1,1': { text: '1', font: { bold: 0 } } } });
    expect(picture).toMatchObject({ type: 'Image', fill: { image: btoa('hello') }, left: 500, top: 100, width: 100,
      line: { visible: 0 } });
    expect([...wps.files.keys()]).toEqual([]);
  });

  it('edit slides through their packages: one saved copy per call, each slide isolated, the copy put in its place', async () => {
    const { answer, deck, wps } = pane();
    const result = await answer('find_and_replace', { pairs: [{ find: 'Atlasware', replace: 'Northwind' }] });
    const [copy, second] = deck.ids();
    expect(copy).not.toBe('256#');
    expect(second).toBe('257#');
    expect(result).toMatchObject({ success: true, result: { total_replacements: 1, slides_modified: 1,
      slides: [{ slide_id: String(parseInt(copy, 10)), position: 1 }] } });
    expect(deck.slides.map(slide => slide.shapes[0])).toMatchObject([{ id: '2', text: 'Northwind grew' }, { text: 'Plan' }]);
    expect(wps.saves).toBe(1);
    expect(wps.inserts).toEqual([{ index: 1, first: 1, last: 1 }]);
    expect([...wps.files.keys()]).toEqual([]);
  });

  it('edit a slide that links to another slide: the copy keeps its own link and drops the one to the other slide', async () => {
    const { answer, deck, wps } = pane();
    const save = wps.presentation.SaveCopyAs as (path: string, format: number) => void;
    wps.presentation.SaveCopyAs = (path: string, format: number) => {
      save(path, format);
      const parts = unzipSync(wps.files.get(path)!);
      const edit = (part: string, change: (xml: string) => string) => { parts[part] = strToU8(change(strFromU8(parts[part]))); };
      edit('ppt/slides/_rels/slide2.xml.rels', xml => xml.replace('</Relationships>', `${['slide1', 'slide2']
        .map(target => `<Relationship Id="r${target}" Type="${SLIDE_RELATIONSHIP}" Target="${target}.xml"/>`).join('')}</Relationships>`));
      edit('ppt/slides/slide2.xml', xml => xml.replace(/(<p:cNvPr id="2"[^>]*)\/>/, '$1><a:hlinkClick r:id="rslide1" '
        + 'action="ppaction://hlinksldjump"/><a:hlinkMouseOver r:id="rslide2" action="ppaction://hlinksldjump"/></p:cNvPr>'));
      wps.files.set(path, zipSync(parts));
    };
    const inserted: Record<string, string>[] = [];
    const insert = wps.presentation.Slides.InsertFromFile as (...args: unknown[]) => unknown;
    wps.presentation.Slides.InsertFromFile = (...args: unknown[]) => {
      inserted.push(Object.fromEntries(Object.entries(unzipSync(wps.files.get(args[0] as string)!))
        .map(([path, bytes]) => [path, strFromU8(bytes)])));
      return insert(...args);
    };

    expect(await answer('find_and_replace', { pairs: [{ find: 'Plan', replace: 'Roadmap' }] }))
      .toMatchObject({ success: true, result: { total_replacements: 1 } });
    expect(deck.slides.map(slide => slide.shapes[0].text)).toEqual(['Atlasware grew', 'Roadmap']);
    const [copy] = inserted;
    expect(Object.keys(copy).filter(path => /^ppt\/slides\/slide\d+\.xml$/.test(path))).toEqual(['ppt/slides/slide1.xml']);
    expect(copy['ppt/slides/slide1.xml']).not.toContain('hlinkClick');
    expect(copy['ppt/slides/slide1.xml']).toContain('<a:hlinkMouseOver r:id="rslide2"');
    expect(copy['ppt/slides/_rels/slide1.xml.rels']).toContain(`Id="rslide2" Type="${SLIDE_RELATIONSHIP}" Target="slide1.xml"`);
    expect(copy['ppt/slides/_rels/slide1.xml.rels']).not.toContain('rslide1');
  });

  it('import chosen slides as contiguous InsertFromFile runs after the slide named, with this deck\'s theme only', async () => {
    const { answer, deck, wps, uploaded } = pane();
    const { asset, downloads } = await uploaded(['One', 'Two', 'Three', 'Four']);
    const result = await answer('import_slides', { asset, source_positions: [1, 2, 4], after_slide_id: '256' }, downloads);
    expect(wps.inserts).toEqual([{ index: 1, first: 1, last: 2 }, { index: 3, first: 4, last: 4 }]);
    expect(deck.slides.map(slide => slide.shapes[0].text)).toEqual(['Atlasware grew', 'One', 'Two', 'Four', 'Plan']);
    const ids = deck.ids().slice(1, 4).map(id => String(parseInt(id, 10)));
    expect(result).toMatchObject({ success: true, result: { slides: [{ source_position: 1, slide_id: ids[0] },
      { source_position: 2, slide_id: ids[1] }, { source_position: 4, slide_id: ids[2] }] } });

    expect(await answer('import_slides', { asset, formatting: 'keep_source_formatting' }, downloads)).toMatchObject({
      success: false, error: expect.stringContaining('WPS Presentation cannot keep the source formatting') });
    expect(deck.slides).toHaveLength(5);
  });

  it('take out the slides of the runs that landed when a later InsertFromFile run fails', async () => {
    const { answer, deck, wps, uploaded } = pane();
    const { asset, downloads } = await uploaded(['One', 'Two', 'Three', 'Four']);
    const insert = wps.presentation.Slides.InsertFromFile;
    wps.presentation.Slides.InsertFromFile = (...args: unknown[]) => {
      if (wps.inserts.length) throw new Error('The disk is full');
      return insert(...args);
    };
    const result = await answer('import_slides', { asset, source_positions: [1, 2, 4] }, downloads);
    expect(result).toMatchObject({ success: false, error: expect.stringContaining('nothing of it remains') });
    expect(deck.slides.map(slide => slide.shapes[0].text)).toEqual(['Atlasware grew', 'Plan']);
  });

  it('refuse code that changes the theme, a master or a layout, which InsertFromFile would replace with the deck\'s', async () => {
    const { answer, deck, wps } = pane();
    const before = deck.content();
    const recolour = `const path = 'ppt/theme/theme1.xml';
      zip.file(path, (await zip.file(path).async('string')).replace('4472C4', 'C00000'));
      markDirty();`;
    expect(await answer('edit_slide_xml', { slide_id: '256', code: recolour })).toMatchObject({ success: false,
      error: expect.stringContaining('ppt/theme/theme1.xml') });
    expect(deck.content()).toEqual(before);
    expect(wps.inserts).toEqual([]);
  });
});
