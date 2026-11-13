import { describe, expect, it, vi } from 'vitest';

import type { ClientToolResult } from './clientAttachment';
import { FakeDeck, uploadedDeck, type FakeDeckOptions } from './fakePowerPoint';
import type { JsonObject } from './gatewayClient';
import { HostWriteQueue } from './hostWriteQueue';
import { nodeSandbox } from './nodeSandbox';
import { powerPointPackageRuntime } from './powerPointPackageTools';
import { powerPointReadRuntime } from './powerPointReadTools';
import { joinRuntimes, PowerPointToolDispatcher } from './powerPointToolDispatcher';
import { powerPointSlideDeck, powerPointWriteRuntime } from './powerPointWriteTools';
import { sha256Hex } from './presentationAssetClient';
import { presentationAttachDeclaration } from './presentationContract';
import { PRESENTATION_TOOL_NAMES } from './presentationToolTypes';
import { SlideSnapshots } from './slideSnapshots';

const NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
  + 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
  + 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"';
const TEXT = (id: string, text: string, extra: JsonObject = {}) => ({ id, name: `Text ${id}`, type: 'TextBox', text,
  left: 40, top: 40, width: 400, height: 60, ...extra });
const PICTURE = `<p:pic ${NS}><p:nvPicPr><p:cNvPr id="7" name="Logo" descr="Brand logo"/><p:cNvPicPr/><p:nvPr/>`
  + '</p:nvPicPr><p:blipFill><a:blip r:embed="rImage"/></p:blipFill><p:spPr><a:xfrm><a:off x="127000" y="127000"/>'
  + '<a:ext cx="1270000" cy="635000"/></a:xfrm></p:spPr></p:pic>';
const COLUMN = { type: 'column', categories: ['A', 'B'], values: [10, 20] };
const TABLE = `<p:graphicFrame ${NS}><p:nvGraphicFramePr><p:cNvPr id="5" name="Table"/><p:cNvGraphicFramePr/>`
  + '<p:nvPr/></p:nvGraphicFramePr><p:xfrm><a:off x="0" y="0"/><a:ext cx="1270000" cy="635000"/></p:xfrm>'
  + '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl><a:tblGrid>'
  + '<a:gridCol w="1270000"/></a:tblGrid><a:tr h="317500"><a:tc><a:txBody><a:bodyPr/><a:p><a:r><a:t>Atlasware'
  + '</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>';

function twoSlides(): FakeDeckOptions {
  return { slides: [
    { id: '256#', shapes: [TEXT('2', 'Atlasware grew'), TEXT('3', 'Plan', { frame: { autoSizeSetting: 'AutoSizeShapeToFitText' } })],
      notes: 'Say Atlasware twice' },
    { id: '257#', shapes: [TEXT('2', 'ATLASWARE wins')] },
  ] };
}

const sandbox = nodeSandbox();

/** The pane's runtime on a fake deck, as the task pane wires it. */
function pane(deck = new FakeDeck(twoSlides())) {
  const writes = new HostWriteQueue();
  const snapshots = new SlideSnapshots(powerPointSlideDeck(deck.run), writes);
  const fetcher = vi.fn(async (_url: string, _init: RequestInit) => new Response('hello'));
  const runtime = joinRuntimes(
    powerPointReadRuntime({ run: deck.run, host: deck.host, documentName: async () => 'Deck.pptx',
      languages: { displayLanguage: 'zh-CN', contentLanguage: 'zh-CN' } }),
    powerPointWriteRuntime({ run: deck.run, host: deck.host, journal: id => snapshots.journal(id) }),
    powerPointPackageRuntime({ run: deck.run, host: deck.host, journal: id => snapshots.journal(id), sandbox,
      forgetUndo: () => snapshots.forget(), fetcher }));
  const dispatcher = new PowerPointToolDispatcher(runtime, writes);
  let calls = 0;
  const call = async (toolName: string, input: JsonObject = {}, downloads: Record<string, string> = {}) => {
    calls += 1;
    return answer(await dispatcher.execute({ requestId: `c${calls}`, interruptId: 'i', toolName, input, downloads }));
  };
  const callIds = () => Array.from({ length: calls }, (_call, at) => `c${at + 1}`);
  /** Ends the turn: how many slides its undo puts back, as the undo icon under its reply says. */
  const endTurn = async () => {
    await snapshots.closeTurn();
    return snapshots.undoable(callIds())?.slides;
  };
  /** Ends the turn and undoes it. */
  const undoTurn = async () => {
    await snapshots.closeTurn();
    return snapshots.undo(callIds());
  };
  return { deck, runtime, snapshots, fetcher, call, endTurn, undoTurn };
}

function answer(result: ClientToolResult): JsonObject {
  const [block] = result.content;
  if (block.type !== 'text') throw new Error('no text block');
  return JSON.parse(block.text) as JsonObject;
}

describe('the slide package tools', () => {
  it('are declared on a host with PowerPointApi 1.8, and not below', () => {
    const { runtime } = pane();
    const declared = (api: string) => presentationAttachDeclaration({ documentId: 'd' as never, displayName: 'x',
      host: new FakeDeck({ api }).host, implemented: runtime.tools }).capabilities.supported_tools;
    const tools = ['read_slide_text', 'edit_slide_text', 'find_and_replace', 'duplicate_slide', 'import_slides',
      'copy_image_between_slides', 'read_chart_style', 'create_slide_chart', 'edit_built_chart', 'edit_slide_xml',
      'edit_slide_chart', 'edit_slide_master'];
    expect(declared('1.8')).toEqual(expect.arrayContaining(tools));
    expect(declared('1.7').filter(tool => tools.includes(tool))).toEqual([]);
  });

  it('declare the tools that run code only where the pane can start a worker', () => {
    const { runtime } = pane();
    const host = { ...new FakeDeck().host, worker: false };
    const declared = presentationAttachDeclaration({ documentId: 'd' as never, displayName: 'x', host,
      implemented: runtime.tools }).capabilities.supported_tools;
    expect(declared).toContain('edit_slide_text');
    expect(declared.filter(tool => ['edit_slide_xml', 'edit_slide_chart', 'edit_slide_master'].includes(tool)))
      .toEqual([]);
  });

  it('refuse an input their schema does not allow, naming where, before they touch the deck', async () => {
    const { deck, call } = pane();
    const before = deck.content();
    expect(await call('create_slide_chart', { slide_id: '256#', spec: { ...COLUMN, colour: 'red' } })).toMatchObject({
      success: false, code: 'INVALID_ARGUMENT', error: expect.stringMatching(/^spec\.colour is not a field here/),
      mutation: { state: 'not_applied', native_mutation_started: false } });
    expect(deck.content()).toEqual(before);
    expect(deck.insertions).toEqual([]);
  });
});

describe('what the tools answer', () => {
  it('points only to tools this pane declares', async () => {
    const options = twoSlides();
    options.slides![0].shapes!.push({ id: '5', type: 'Table', xml: TABLE }, { id: '7', type: 'Image', xml: PICTURE });
    const { deck, runtime, call } = pane(new FakeDeck(options));
    await call('create_slide_chart', { slide_id: '257#', spec: COLUMN });
    const chart = deck.slides[1].parts!['ppt/charts/chart1.xml'];
    chart.base64 = btoa(atob(chart.base64).replace(/c:barChart/g, 'c:pieChart'));
    const answers = [
      await call('read_slide_text', { ref: '256#:5' }),
      await call('read_slide_text', { ref: `${deck.ids()[1]}:3` }),
      await call('edit_slide_text', { ref: '256#:7', code: '<a:p/>' }),
      await call('find_and_replace', { pairs: [{ find: 'A', replace: 'x' }, { find: 'master', replace: 'x' }],
        dry_run: true }),
      await call('edit_built_chart', { slide_id: deck.ids()[1], spec: COLUMN }),
    ];
    const named = PRESENTATION_TOOL_NAMES.filter(name => new RegExp(`\\b${name}\\b`).test(JSON.stringify(answers)));
    expect(named.filter(name => !runtime.tools.has(name))).toEqual([]);
  });
});

describe('read_slide_text and edit_slide_text', () => {
  it('reads a shape\'s paragraphs as XML', async () => {
    const { call } = pane();
    expect(await call('read_slide_text', { ref: '256#:2' })).toEqual({ success: true,
      result: `<a:p ${NS.split(' ')[0]}><a:r><a:t>Atlasware grew</a:t></a:r></a:p>` });
    expect(await call('read_slide_text', { ref: '256#:9' })).toMatchObject({ code: 'INVALID_ARGUMENT',
      error: expect.stringMatching(/Shape 9 is not on this slide/) });
    expect(await call('read_slide_text', { ref: '256#' })).toMatchObject({ code: 'INVALID_ARGUMENT' });
  });

  it('puts an edited copy in the slide\'s place, keeping its shape ids, and names the copy', async () => {
    const { deck, call } = pane();
    const result = await call('edit_slide_text', { ref: '256#:2', code: '<a:p><a:r><a:rPr b="1"/><a:t>Up</a:t></a:r>'
      + '<a:br/><a:r><a:t>again</a:t></a:r></a:p>' });
    const [copy, other] = deck.ids();
    expect(other).toBe('257#');
    expect(result).toMatchObject({ success: true, result: { ref: `${copy}:2` },
      mutation: { state: 'applied', replacement_slide_ids: { '256#': copy }, verification_targets: [copy] } });
    expect(deck.slide(copy).shapes.map(shape => shape.id)).toEqual(['2', '3']);
    expect(deck.slide(copy).shapes[0]).toMatchObject({ text: 'Up\vagain', name: 'Text 2', left: 40 });
    expect(deck.slide(copy).notes).toBe('Say Atlasware twice');
    expect(result.structure_fingerprint_after)
      .toBe((await call('read_presentation_context')).structure_fingerprint);
  });

  it('fits the frame of a shape that fits its text, and leaves the others', async () => {
    const { deck, call } = pane();
    await call('edit_slide_text', { ref: '256#:3', code: '<a:p><a:r><a:t>A much longer plan</a:t></a:r></a:p>' });
    await call('edit_slide_text', { ref: '257#:2', code: '<a:p><a:r><a:t>Short</a:t></a:r></a:p>' });
    expect(deck.refits).toEqual(['3']);
  });

  it('edits a shape inside a group, which has no frame of its own to fit again', async () => {
    const group = `<p:grpSp ${NS}><p:nvGrpSpPr><p:cNvPr id="10" name="Group"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>`
      + '<p:grpSpPr/><p:sp><p:nvSpPr><p:cNvPr id="11" name="Child"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/>'
      + '<p:txBody><a:bodyPr/><a:p><a:r><a:t>old</a:t></a:r></a:p></p:txBody></p:sp></p:grpSp>';
    const { deck, call } = pane(new FakeDeck({ slides: [{ id: '256#', shapes: [{ id: '10', type: 'Group', xml: group }] }] }));
    const result = await call('edit_slide_text', { ref: '256#:11', code: '<a:p><a:r><a:t>new</a:t></a:r></a:p>' });
    const [copy] = deck.ids();
    expect(result).toMatchObject({ success: true, result: { ref: `${copy}:11` } });
    expect(deck.slide(copy).shapes[0].xml).toContain('<a:t>new</a:t>');
    expect(deck.refits).toEqual([]);
  });

  it('is undone with the turn as one slide, leaving the deck as it was', async () => {
    const { deck, call, endTurn, undoTurn } = pane();
    const before = deck.content();
    await call('edit_slide_text', { ref: '256#:2', code: '<a:p><a:r><a:t>One</a:t></a:r></a:p>' });
    await call('edit_slide_text', { ref: `${deck.ids()[0]}:2`, code: '<a:p><a:r><a:t>Two</a:t></a:r></a:p>' });
    expect(await endTurn()).toBe(1);
    expect(await undoTurn()).toMatchObject({ kind: 'undone', turns: 1, positions: [1] });
    expect(deck.content()).toEqual(before);
  });

  it('puts the slide back when PowerPoint fails to insert the copy', async () => {
    const { deck, call } = pane();
    const before = deck.content();
    deck.insertsBeforeFailure = 0;
    expect(await call('edit_slide_text', { ref: '256#:2', code: '<a:p><a:r><a:t>Lost</a:t></a:r></a:p>' }))
      .toMatchObject({ success: false, code: 'HOST_WRITE_FAILED', details: { outcome: 'rolled_back' },
        mutation: { state: 'not_applied' } });
    expect(deck.content()).toEqual(before);
  });

  it('refuses paragraphs that are not paragraphs, changing nothing', async () => {
    const { deck, call } = pane();
    const before = deck.content();
    expect(await call('edit_slide_text', { ref: '256#:2', code: '<a:r><a:t>x</a:t></a:r>' })).toMatchObject({
      code: 'INVALID_ARGUMENT', error: expect.stringMatching(/<a:p> paragraphs/), mutation: { state: 'not_applied' } });
    expect(deck.content()).toEqual(before);
  });
});

describe('find_and_replace', () => {
  const RENAME = { pairs: [{ find: 'atlasware', replace: 'Nova' }], preserve_case: true };

  it('replaces across the slides and their notes, and reports where', async () => {
    const { deck, call } = pane();
    const result = await call('find_and_replace', RENAME);
    const [first, second] = deck.ids();
    expect(result).toMatchObject({ success: true, result: { dry_run: false, total_replacements: 3, slides_modified: 2,
      pairs: [{ find: 'atlasware', replace: 'Nova', replacements: 3 }],
      slides: [{ slide_id: first, position: 1, replacements: 2 }, { slide_id: second, position: 2, replacements: 1 }] },
    mutation: { state: 'applied', replacement_slide_ids: { '256#': first, '257#': second } } });
    expect(deck.slides.map(slide => slide.shapes[0].text)).toEqual(['Nova grew', 'NOVA wins']);
    expect(deck.slide(first).notes).toBe('Say Nova twice');
    expect(deck.refits).toEqual([]);
  });

  it('has the rows of a table it changed take their heights again, where the host has Table.rows', async () => {
    const refits = async (api: string) => {
      const { deck, call } = pane(new FakeDeck({ api, slides: [{ shapes: [{ id: '5', type: 'Table', xml: TABLE }] }] }));
      expect(await call('find_and_replace', RENAME)).toMatchObject({ success: true,
        result: { slides: [{ locations: [{ container: 'table', shape_id: '5', cell: 'r0c0' }] }] } });
      return deck.refits;
    };
    expect(await refits('1.9')).toEqual(['5']);
    expect(await refits('1.8')).toEqual([]);
  });

  it('only counts on a dry run, and changes nothing', async () => {
    const { deck, call, endTurn } = pane();
    const before = deck.content();
    expect(await call('find_and_replace', { ...RENAME, dry_run: true, slide_ids: ['257#'] })).toMatchObject({
      success: true, result: { dry_run: true, total_replacements: 1, slides_modified: 0,
        warnings: [expect.stringMatching(/^No slide came with speaker notes/)] },
      mutation: { state: 'not_applied', native_mutation_started: false } });
    expect(deck.ids()).toEqual(['256#', '257#']);
    expect(deck.content()).toEqual(before);
    expect(await endTurn()).toBeUndefined();
  });

  it('is undone with the turn, every slide it changed put back once', async () => {
    const { deck, call, endTurn, undoTurn } = pane();
    const before = deck.content();
    await call('find_and_replace', RENAME);
    expect(await endTurn()).toBe(2);
    expect(await undoTurn()).toMatchObject({ kind: 'undone', turns: 1, positions: [1, 2] });
    expect(deck.content()).toEqual(before);
  });

  it('puts every slide back when PowerPoint fails part way', async () => {
    const { deck, call } = pane();
    const before = deck.content();
    deck.insertsBeforeFailure = 1;
    const failed = await call('find_and_replace', RENAME);
    expect(failed).toMatchObject({ success: false, code: 'HOST_WRITE_FAILED', details: { outcome: 'rolled_back' } });
    expect(deck.content()).toEqual(before);
    expect(failed.details).toMatchObject({ restored_slide_ids: { '256#': deck.ids()[0], '257#': deck.ids()[1] } });
    expect(Object.keys((failed.details as { restored_slide_ids: object }).restored_slide_ids))
      .toEqual(['256#', '257#']);
  });

  it('reports a match in the layout the slides use and in the master once, not once per slide or layout', async () => {
    const { call } = pane();
    const { result } = await call('find_and_replace', { pairs: [{ find: 'master', replace: 'x' }] }) as {
      result: { total_replacements: number; skipped: { reason: string; detail: string }[] } };
    expect(result.total_replacements).toBe(0);
    expect(result.skipped.sort((first, second) => first.reason.localeCompare(second.reason))).toEqual([
      { reason: 'layout-text',
        detail: expect.stringMatching(/^"master" occurs 1x in the slide layout \(ppt\/slideLayouts\/slideLayout1\.xml\)/) },
      { reason: 'master-text', detail: expect.stringMatching(/^"master" occurs 1x in the slide master/) }]);
  });

  it('refuses a slide the deck does not have', async () => {
    const { call } = pane();
    expect(await call('find_and_replace', { ...RENAME, slide_ids: ['999#'] })).toMatchObject({
      code: 'INVALID_ARGUMENT', error: expect.stringMatching(/Slide 999# is not in this presentation/) });
  });

  it('skips a slide whose package it refuses, and replaces on the others', async () => {
    const options = twoSlides();
    options.slides![0].parts = { 'ppt/media/odd%name.png': { contentType: 'image/png', base64: btoa('png'),
      relationshipId: 'rImage', relationshipType: 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/image' } };
    const { deck, call } = pane(new FakeDeck(options));
    expect(await call('find_and_replace', RENAME)).toMatchObject({ success: true, result: { slides_modified: 1,
      slides: [{ slide_id: deck.ids()[1], position: 2 }], skipped: [{ reason: 'slide-package-refused',
        detail: 'slide 1: The slide package has an unsafe part path: "ppt/media/odd%name.png"' }] } });
    expect(deck.slides.map(slide => slide.shapes[0].text)).toEqual(['Atlasware grew', 'NOVA wins']);
  });
});

describe('duplicate_slide', () => {
  it('puts the copy right after its slide, and the undo takes it out', async () => {
    const { deck, call, endTurn, undoTurn } = pane();
    const before = deck.content();
    const result = await call('duplicate_slide', { slide_id: '256#' });
    const copy = deck.ids()[1];
    expect(result).toMatchObject({ success: true, result: { slide_id: copy, source_slide_id: '256#' },
      mutation: { state: 'applied', created_slide_ids: [copy] } });
    expect(deck.content()[1]).toEqual(deck.content()[0]);
    expect(await endTurn()).toBe(1);
    expect(await undoTurn()).toMatchObject({ kind: 'undone' });
    expect(deck.content()).toEqual(before);
    expect(deck.ids()).toEqual(['256#', '257#']);
  });
});

describe('import_slides', () => {
  async function upload(titles: string[], extra: Record<string, string> = {}) {
    const bytes = uploadedDeck(titles, extra);
    return { asset: { handle: 'oa_deck', purpose: 'input_pptx', size: bytes.length, sha256: await sha256Hex(bytes),
      mime_type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation' }, bytes };
  }
  const downloads = { oa_deck: '/file-api/download?token=t' };

  it('inserts the chosen slides in their source order after the slide named, and the undo takes them out', async () => {
    const { deck, call, fetcher, undoTurn } = pane();
    const { asset, bytes } = await upload(['One', 'Two', 'Three']);
    fetcher.mockImplementation(async () => new Response(bytes));
    const before = deck.content();
    const result = await call('import_slides', { asset, source_positions: [3, 1, 3], after_slide_id: '256#',
      formatting: 'keep_source_formatting' }, downloads);
    const ids = deck.ids();
    expect(result).toMatchObject({ success: true, result: { slides: [{ source_position: 1, slide_id: ids[1] },
      { source_position: 3, slide_id: ids[2] }] }, mutation: { created_slide_ids: [ids[1], ids[2]] } });
    expect(deck.slides.map(slide => slide.shapes[0].text)).toEqual(['Atlasware grew', 'One', 'Three', 'ATLASWARE wins']);
    expect(deck.insertions).toEqual([{ formatting: 'KeepSourceFormatting', targetSlideId: '256#',
      sourceSlideIds: ['260#', '262#'] }]);
    await undoTurn();
    expect(deck.content()).toEqual(before);
  });

  it('refuses a position past the file\'s slides, and a file that did not download, changing nothing', async () => {
    const { deck, call, fetcher } = pane();
    const { asset, bytes } = await upload(['One']);
    fetcher.mockImplementation(async () => new Response(bytes));
    expect(await call('import_slides', { asset, source_positions: [2] }, downloads)).toMatchObject({
      code: 'INVALID_ARGUMENT', error: 'source_positions has 2, but the .pptx has 1 slides' });
    expect(await call('import_slides', { asset, after_slide_id: '999#' }, downloads)).toMatchObject({
      code: 'INVALID_ARGUMENT', error: expect.stringMatching(/Slide 999# is not in this presentation/) });
    expect(await call('import_slides', { asset }, {})).toMatchObject({ code: 'ASSET_DOWNLOAD_FAILED',
      mutation: { state: 'not_applied' } });
    expect(deck.insertions).toEqual([]);
  });

  it('refuses a slide that holds an OLE object, changing nothing', async () => {
    const { deck, call, fetcher } = pane();
    const { asset, bytes } = await upload(['One', 'Two'], { 'ppt/slides/_rels/slide2.xml.rels': '<Relationships '
      + 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://'
      + 'schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" Target="../embeddings/a.bin"/>'
      + '</Relationships>' });
    fetcher.mockImplementation(async () => new Response(bytes));
    expect(await call('import_slides', { asset }, downloads)).toMatchObject({ code: 'INVALID_ARGUMENT',
      error: 'Slide 2 of the .pptx holds an OLE object (ppt/embeddings/a.bin), which import_slides does not bring in',
      mutation: { state: 'not_applied' } });
    expect(await call('import_slides', { asset, source_positions: [1] }, downloads)).toMatchObject({ success: true });
    expect(deck.insertions).toEqual([expect.objectContaining({ sourceSlideIds: ['260#'] })]);
  });

  it('appends every slide of the file, in the deck\'s theme, when told nothing else', async () => {
    const { deck, call, fetcher } = pane();
    const { asset, bytes } = await upload(['One', 'Two']);
    fetcher.mockImplementation(async () => new Response(bytes));
    await call('import_slides', { asset }, downloads);
    expect(deck.slides.map(slide => slide.shapes[0].text)).toEqual(['Atlasware grew', 'ATLASWARE wins', 'One', 'Two']);
    expect(deck.insertions).toEqual([{ formatting: 'UseDestinationTheme', targetSlideId: '257#',
      sourceSlideIds: ['260#', '261#'] }]);
  });
});

describe('copy_image_between_slides', () => {
  it('copies the picture with its image onto the other slide', async () => {
    const options = twoSlides();
    options.slides![0].shapes!.push({ id: '7', name: 'Logo', type: 'Image', xml: PICTURE });
    options.slides![0].parts = { 'ppt/media/image1.png': { contentType: 'image/png', base64: btoa('png'),
      relationshipId: 'rImage', relationshipType: 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/image' } };
    const { deck, call, undoTurn } = pane(new FakeDeck(options));
    const before = deck.content();
    const result = await call('copy_image_between_slides', { from_slide_id: '256#', from_shape_id: '7',
      to_slide_id: '257#', x: 100 });
    const target = deck.slides[1];
    expect(result).toMatchObject({ success: true, result: { ref: `${target.id}:3` },
      mutation: { replacement_slide_ids: { '257#': target.id } } });
    expect(target.shapes[1]).toMatchObject({ id: '3', name: 'Picture 3', type: 'Image', left: 100, top: 10 });
    expect(Object.values(target.parts!)).toEqual([expect.objectContaining({ base64: btoa('png') })]);
    await undoTurn();
    expect(deck.content()).toEqual(before);
  });
});

describe('edit_slide_xml and edit_slide_chart', () => {
  /** Code that changes the text ``from`` on the slide to ``to``. */
  const retext = (from: string, to: string) => `const path = 'ppt/slides/slide1.xml';
    const xml = await zip.file(path).async('string');
    zip.file(path, xml.replace('${from}', escapeXml('${to}')));
    markDirty();`;

  it('run the code on the slide\'s package and put what it marked dirty in the slide\'s place', async () => {
    const { deck, call } = pane();
    const result = await call('edit_slide_xml', { slide_id: '256#',
      code: `${retext('Plan', 'Plan & budget')}\nreturn 'done';`, autosize_shape_ids: ['3'] });
    const [copy] = deck.ids();
    expect(result).toMatchObject({ success: true, result: { slide_id: copy, script_result: 'done' },
      mutation: { state: 'applied', replacement_slide_ids: { '256#': copy } } });
    expect(deck.slide(copy).shapes.map(shape => [shape.id, shape.text])).toEqual([['2', 'Atlasware grew'],
      ['3', 'Plan & budget']]);
    expect(deck.refits).toEqual(['3']);
    expect(await call('edit_slide_chart', { slide_id: copy, code: retext('budget', 'costs') }))
      .toMatchObject({ success: true, mutation: { state: 'applied' } });
    expect(deck.slides[0].shapes[1].text).toBe('Plan & costs');
  });

  it('change nothing when the code only reads, or forgets markDirty, and say which', async () => {
    const { deck, call } = pane();
    const before = deck.content();
    expect(await call('edit_slide_xml', { slide_id: '256#', code: 'return Object.keys(zip.files).length > 10;' }))
      .toMatchObject({ success: true, result: { script_result: true }, mutation: { state: 'not_applied' } });
    expect(await call('edit_slide_xml', { slide_id: '256#', code: retext('Plan', 'Lost').replace('markDirty();', '') }))
      .toMatchObject({ success: true, result: { warning: expect.stringMatching(/never called markDirty/) },
        mutation: { state: 'not_applied' } });
    expect(deck.content()).toEqual(before);
    expect(deck.insertions).toEqual([]);
  });

  it('refuse what the code may not put in the deck, and a shape to fit that the slide lacks', async () => {
    const { deck, call } = pane();
    const before = deck.content();
    const writes = {
      macro: 'zip.file(\'ppt/vbaProject.bin\', \'x\');',
      link: 'zip.file(\'ppt/slides/_rels/slide1.xml.rels\', (await zip.file(\'ppt/slides/_rels/slide1.xml.rels\')'
        + '.async(\'string\')).replace(\'</Relationships>\', \'<Relationship Id="rX" Type="http://schemas.'
        + 'openxmlformats.org/officeDocument/2006/relationships/image" Target="https://x.test/a.png" '
        + 'TargetMode="External"/></Relationships>\'));',
      slide: 'zip.remove(\'ppt/slides/slide1.xml\');',
    };
    for (const [what, code] of Object.entries(writes)) {
      expect(await call('edit_slide_xml', { slide_id: '256#', code: `${code}\nmarkDirty();` }), what).toMatchObject({
        success: false, code: 'INVALID_ARGUMENT', mutation: { state: 'not_applied' } });
    }
    expect(await call('edit_slide_xml', { slide_id: '256#', code: retext('Plan', 'Lost'),
      autosize_shape_ids: ['3', '9'] })).toMatchObject({ code: 'INVALID_ARGUMENT',
      error: expect.stringMatching(/names 9, which the edited slide does not/) });
    expect(deck.content()).toEqual(before);
    expect(deck.insertions).toEqual([]);
  });

  it('answer code that fails with what it threw, changing nothing', async () => {
    const { deck, call } = pane();
    const before = deck.content();
    expect(await call('edit_slide_xml', { slide_id: '256#',
      code: 'await zip.file(\'ppt/none.xml\').async(\'string\');' }))
      .toMatchObject({ success: false, code: 'OOXML_SCRIPT_FAILED', error: expect.stringMatching(/^The code failed: /),
        mutation: { state: 'not_applied', native_mutation_started: false } });
    expect(deck.content()).toEqual(before);
  });

  it('are undone with the turn, the slide put back once', async () => {
    const { deck, call, endTurn, undoTurn } = pane();
    const before = deck.content();
    await call('edit_slide_xml', { slide_id: '256#', code: retext('Plan', 'One') });
    await call('edit_slide_xml', { slide_id: deck.ids()[0], code: retext('One', 'Two') });
    expect(await endTurn()).toBe(1);
    expect(await undoTurn()).toMatchObject({ kind: 'undone', positions: [1] });
    expect(deck.content()).toEqual(before);
  });
});

describe('edit_slide_master', () => {
  const RECOLOUR = `const path = 'ppt/theme/theme1.xml';
    zip.file(path, (await zip.file(path).async('string')).replace('4472C4', 'C00000'));
    markDirty();`;
  /** A deck of three slides on one master of three layouts. */
  const masterDeck = (preserve = false) => new FakeDeck({
    masters: [{ id: 'm1', name: 'Office Theme', preserve, layouts: [{ id: 'cover', name: 'Title Slide' },
      { id: 'blank', name: 'Blank' }, { id: 'heading', name: 'Title Only' }] }],
    slides: [{ id: '256#', layoutId: 'cover', shapes: [TEXT('2', 'Cover')] },
      { id: '257#', layoutId: 'blank', shapes: [TEXT('2', 'Body')] },
      { id: '258#', layoutId: 'heading', shapes: [TEXT('2', 'More')] }] });
  const layouts = (deck: FakeDeck) => deck.slides.map(slide => [deck.template(slide).master.name,
    deck.template(slide).layout.name]);

  it('brings the edited master in with the first slide, moves the other slides to it by layout name, and drops the '
    + 'old master', async () => {
    const { deck, call } = pane(masterDeck());
    const result = await call('edit_slide_master', { code: RECOLOUR });
    const [first, ...others] = deck.ids();
    expect(result).toMatchObject({ success: true, result: { slide_id: first,
      master_name: expect.stringMatching(/^Office Theme \[\w+\]$/), ambiguous_layout_slide_ids: [],
      missing_layout_slide_ids: [], old_master_retained: false },
      mutation: { state: 'applied', replacement_slide_ids: { '256#': first },
        affected_slide_ids: [first, ...others] } });
    const name = (result.result as JsonObject).master_name;
    expect(layouts(deck)).toEqual([[name, 'Title Slide'], [name, 'Blank'], [name, 'Title Only']]);
    expect(deck.masters.map(master => master.name)).toEqual([name]);
    expect(deck.slides.map(slide => slide.shapes.map(shape => shape.text))).toEqual([['Cover'], ['Body'], ['More']]);
    expect(await call('read_presentation_context')).toMatchObject({ theme_palette: { accent1: 'C00000' } });
  });

  it('says which slides it could not move, and keeps the old master they still use or that is preserved',
    async () => {
    const { deck, call } = pane(masterDeck());
    const renames = `for (const path of Object.keys(zip.files).filter(path => path.startsWith('ppt/slideLayouts/s'))) {
      const xml = await zip.file(path).async('string');
      zip.file(path, xml.replace('name="Blank"', 'name="Empty"').replace('name="Title Slide"', 'name="Title Only"'));
    }
    markDirty();`;
    expect(await call('edit_slide_master', { code: renames })).toMatchObject({ success: true, result: {
      ambiguous_layout_slide_ids: ['258#'], missing_layout_slide_ids: ['257#'], old_master_retained: true } });
    expect(layouts(deck).slice(1)).toEqual([['Office Theme', 'Blank'], ['Office Theme', 'Title Only']]);

    const preserved = pane(masterDeck(true));
    expect(await preserved.call('edit_slide_master', { code: RECOLOUR })).toMatchObject({ success: true,
      result: { missing_layout_slide_ids: [], old_master_retained: true } });
    expect(preserved.deck.masters).toHaveLength(2);
  });

  it('clears the undo of this turn and the turns before it', async () => {
    const { deck, call, endTurn } = pane(masterDeck());
    await call('edit_slide_text', { ref: '257#:2', code: '<a:p><a:r><a:t>Edited</a:t></a:r></a:p>' });
    expect(await endTurn()).toBe(1);
    await call('edit_slide_master', { code: RECOLOUR });
    await call('edit_slide_text', { ref: `${deck.ids()[1]}:2`, code: '<a:p><a:r><a:t>Again</a:t></a:r></a:p>' });
    expect(await endTurn()).toBeUndefined();
  });

  it('changes nothing when the code only reads', async () => {
    const { deck, call } = pane(masterDeck());
    const before = deck.content();
    const code = 'return Object.keys(zip.files).filter(path => path.includes(\'Layout\')).length;';
    expect(await call('edit_slide_master', { code }))
      .toMatchObject({ success: true, result: { script_result: 6 }, mutation: { state: 'not_applied' } });
    expect(deck.content()).toEqual(before);
    expect(deck.masters).toHaveLength(1);
  });
});

describe('charts', () => {
  it('creates a chart from a spec, reads its style back and draws it again from a new spec', async () => {
    const { deck, call, undoTurn } = pane();
    const before = deck.content();
    const created = await call('create_slide_chart', { slide_id: '257#', spec: { ...COLUMN, construction:
      { legend: 'top' } } });
    let slide = deck.slides[1];
    expect(created).toMatchObject({ success: true, result: { refs: [`${slide.id}:3`], chart_part: 'ppt/charts/chart1.xml' } });
    expect(slide.shapes[1]).toMatchObject({ id: '3', name: 'Chart', type: 'Chart' });
    expect(await call('read_chart_style', { slide_id: slide.id })).toMatchObject({ success: true, result: { charts: [
      { chart_part: 'ppt/charts/chart1.xml', style: { form: 'column', legend: 'top', unsupported: [] } }] } });
    expect(await call('edit_built_chart', { slide_id: slide.id, spec: { ...COLUMN, values: [10, 30] } }))
      .toMatchObject({ success: true, result: { chart_part: 'ppt/charts/chart1.xml' } });
    slide = deck.slides[1];
    expect(atob(slide.parts!['ppt/charts/chart1.xml'].base64)).toContain('<c:v>30</c:v>');
    expect(await call('edit_built_chart', { slide_id: slide.id, chart_index: 1, spec: COLUMN })).toMatchObject({
      code: 'INVALID_ARGUMENT', error: expect.stringMatching(/chart_index 1 is past/) });
    expect(await call('create_slide_chart', { slide_id: slide.id, spec: { ...COLUMN, values: [1] } }))
      .toMatchObject({ code: 'INVALID_ARGUMENT', error: 'spec.values must have at least 2 items' });
    expect(await call('create_slide_chart', { slide_id: slide.id, spec: { ...COLUMN, accent_index: 5 } }))
      .toMatchObject({ code: 'INVALID_ARGUMENT', error: 'accent_index is out of range' });
    expect(await undoTurn()).toMatchObject({ kind: 'undone' });
    expect(deck.content()).toEqual(before);
  });
});

describe('read_presentation_context on a template', () => {
  it('gives the theme palette, fonts and body size from the first slide', async () => {
    const theme = { name: 'Brand', palette: { dk1: '111111', accent1: 'C00000' }, fonts: { major: 'Georgia',
      minor: 'Arial' }, bodyFontPt: 20 };
    const branded = pane(new FakeDeck({ ...twoSlides(), theme,
      masters: [{ id: 'm1', name: 'Brand', layouts: [{ id: 'l1', name: 'Blank' }] }] }));
    expect(await branded.call('read_presentation_context')).toMatchObject({ is_default_theme: false,
      theme_palette: { dk1: '111111', accent1: 'C00000' }, theme_fonts: { major: 'Georgia', minor: 'Arial' },
      template_body_font_pt: 20 });
    expect(await pane().call('read_presentation_context')).toMatchObject({ is_default_theme: true,
      theme_palette: null, theme_fonts: null, template_body_font_pt: null });
  });
});
