import { strFromU8, strToU8, unzipSync, zipSync } from 'fflate';
import { describe, expect, it } from 'vitest';

import {
  copyImage, createChart, editBuiltChart, editSlideText, findAndReplace, importedSlides, PackageRefused,
  readChartStyle, readSlideText, themeOf, type FindOptions,
} from './presentationPackage';

const NS = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
  + 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
  + 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"';
const RELS = 'http://schemas.openxmlformats.org/package/2006/relationships';
const R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
const TYPES = 'http://schemas.openxmlformats.org/package/2006/content-types';
const SLIDE = 'ppt/slides/slide1.xml';
const TITLE = '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/><p:txBody>'
  + '<a:bodyPr anchor="t"/><a:lstStyle/><a:p><a:r><a:rPr b="1"/><a:t>ATLAS</a:t></a:r><a:r><a:rPr i="1"/>'
  + '<a:t>WARE</a:t></a:r></a:p><a:p><a:r><a:t>Cross</a:t></a:r><a:br/><a:r><a:t>Break</a:t></a:r></a:p><a:p>'
  + '<a:fld id="f1" type="slidenum"><a:rPr/><a:t>ATLASWARE</a:t></a:fld></a:p></p:txBody></p:sp>';
const PICTURE = '<p:pic><p:nvPicPr><p:cNvPr id="7" name="Logo" descr="Brand logo"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
  + '<p:blipFill><a:blip r:embed="rId2"><a:extLst><a:ext uri="svg"><asvg:svgBlip '
  + 'xmlns:asvg="http://schemas.microsoft.com/office/drawing/2016/SVG/main" r:embed="rId3"/></a:ext></a:extLst></a:blip>'
  + '<a:srcRect l="1000" r="2000"/></p:blipFill><p:spPr><a:xfrm><a:off x="12700" y="25400"/><a:ext cx="38100" '
  + 'cy="50800"/></a:xfrm></p:spPr></p:pic>';
const FIND: FindOptions = { match: 'substring', case_sensitive: false, preserve_case: true, include_notes: true,
  dry_run: false };
const COLUMN = { type: 'column', categories: ['A', 'B'], values: [10, 20] };

/** A slide package as exportAsBase64 gives one: the slide, and the parts it names. */
function slidePackage({ shapes = TITLE, parts = {}, rels = '', types = '', size = [12192000, 6858000] }: {
  shapes?: string; parts?: Record<string, string | Uint8Array>; rels?: string; types?: string; size?: number[];
} = {}): string {
  return pack({
    '[Content_Types].xml': `<Types xmlns="${TYPES}"><Default Extension="png" ContentType="image/png"/>${types}</Types>`,
    'ppt/presentation.xml': `<p:presentation ${NS}><p:sldSz cx="${size[0]}" cy="${size[1]}"/></p:presentation>`,
    [SLIDE]: `<p:sld ${NS}><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/>`
      + `</p:nvGrpSpPr><p:grpSpPr/>${shapes}</p:spTree></p:cSld></p:sld>`,
    'ppt/slides/_rels/slide1.xml.rels': `<Relationships xmlns="${RELS}"><Relationship Id="rId1" `
      + `Type="${RELS}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>${rels}</Relationships>`,
    ...parts,
  });
}

function pack(parts: Record<string, string | Uint8Array>): string {
  const bytes = zipSync(Object.fromEntries(Object.entries(parts)
    .map(([path, part]) => [path, typeof part === 'string' ? strToU8(part) : part])));
  return Buffer.from(bytes).toString('base64');
}

function unpack(base64: string): Record<string, Uint8Array> {
  return unzipSync(Buffer.from(base64, 'base64'));
}

function part(base64: string, path: string): string {
  return strFromU8(unpack(base64)[path]);
}

function withParts(base64: string, parts: Record<string, string | Uint8Array>): string {
  return pack({ ...unpack(base64), ...parts });
}

describe('find_and_replace on a slide package', () => {
  const notes = `<p:notes ${NS}><p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id="3" name="Notes"/></p:nvSpPr><p:txBody>`
    + '<a:p><a:r><a:t>atlasware note</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>';
  const chart = '<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart><c:plotArea>'
    + '<c:barChart><c:ser><c:tx><c:v>ATLASWARE chart</c:v></c:tx></c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>';
  const original = slidePackage({ parts: { 'ppt/notesSlides/notesSlide1.xml': notes, 'ppt/charts/chart1.xml': chart } });

  it('keeps the first run\'s formatting, searches the notes, and reports what it leaves', () => {
    const edit = findAndReplace(original, [{ find: 'atlasware', replace: 'nova' }, { find: 'Cross\nBreak',
      replace: 'joined' }], FIND);
    expect(edit.changed).toBe(true);
    expect(edit.found).toMatchObject({ replacements: 2, perPair: [2, 0], changedShapeIds: ['2'], notesFound: true,
      locations: [{ container: 'body', replacements: 1, shape_id: '2', shape_name: 'Title' },
        { container: 'notes', replacements: 1, shape_id: '3' }] });
    expect(edit.found.skipped.map(item => item.reason).sort()).toEqual(['chart-text', 'field-text', 'line-break-span']);
    const slide = part(edit.base64, SLIDE);
    expect(slide).toContain('<a:p><a:r><a:rPr b="1"/><a:t>NOVA</a:t></a:r></a:p>');
    expect(slide).toContain('<a:fld id="f1" type="slidenum"><a:rPr/><a:t>ATLASWARE</a:t></a:fld>');
    expect(part(edit.base64, 'ppt/notesSlides/notesSlide1.xml')).toContain('<a:t>nova note</a:t>');
  });

  it('leaves a match that runs across a field, which has no text of its own to replace', () => {
    const paged = slidePackage({ shapes: TITLE.replace('<a:p><a:fld', '<a:p><a:r><a:t>Page </a:t></a:r><a:fld')
      .replace('</a:fld></a:p>', '</a:fld><a:r><a:t> total</a:t></a:r></a:p>') });
    const edit = findAndReplace(paged, [{ find: 'Page  total', replace: 'x' }], FIND);
    expect(edit).toMatchObject({ changed: false, found: { replacements: 0, skipped: [{ reason: 'line-break-span',
      detail: '1 occurrences span a line break or a field and were not replaced' }] } });
  });

  it('matches whole words against ASCII letters and digits only, so it finds a term inside Chinese text', () => {
    const chinese = slidePackage({ shapes: TITLE.replace('<a:t>Cross</a:t>', '<a:t>使用Atlas平台和Atlas2</a:t>') });
    const edit = findAndReplace(chinese, [{ find: 'atlas', replace: 'Nova' }],
      { ...FIND, match: 'whole_word', preserve_case: false });
    expect(edit.found.replacements).toBe(1);
    expect(part(edit.base64, SLIDE)).toContain('<a:t>使用Nova平台和Atlas2</a:t>');
  });

  it('only counts on a dry run, and leaves the package as it came', () => {
    const edit = findAndReplace(original, [{ find: 'ATLASWARE', replace: 'NOVA' }],
      { ...FIND, match: 'whole_word', dry_run: true });
    expect(edit).toMatchObject({ changed: false, base64: original, found: { replacements: 2 } });
  });

  it('leaves the notes out when told to', () => {
    const edit = findAndReplace(original, [{ find: 'note', replace: 'memo' }], { ...FIND, include_notes: false });
    expect(edit).toMatchObject({ changed: false, found: { replacements: 0, notesFound: false } });
  });
});

describe('the safety of a slide package', () => {
  it('refuses part paths outside plain OPC paths', () => {
    for (const path of ['ppt/slides/slide2.xml.piece', 'ppt/slides/%2e%2e/escape.xml', 'ppt\\slides\\x.xml']) {
      expect(() => readSlideText(withParts(slidePackage(), { [path]: '<x/>' }), '2')).toThrow(/unsafe part path/);
    }
    expect(() => readSlideText(Buffer.from('not a zip').toString('base64'), '2')).toThrow(PackageRefused);
  });
});

describe('a shape\'s paragraphs', () => {
  it('reads them as XML, and says what a shape without text holds instead', () => {
    const base64 = slidePackage({ shapes: TITLE + PICTURE });
    expect(readSlideText(base64, '2').split('\n')).toHaveLength(3);
    expect(readSlideText(base64, '2')).toMatch(/^<a:p xmlns:a="[^"]+"><a:r><a:rPr b="1"\/><a:t>ATLAS<\/a:t>/);
    expect(readSlideText(base64, '7')).toMatch(/^\(image: no text body\. Alt text: "Brand logo"/);
    expect(() => readSlideText(base64, '9')).toThrow(/Shape 9 is not on this slide/);
  });

  it('replaces them and keeps the body properties and list style', () => {
    const edit = editSlideText(slidePackage(), '2', '<a:p><a:r><a:rPr sz="1400" b="1"/><a:t>New</a:t></a:r></a:p>');
    expect(part(edit.base64, SLIDE)).toContain('<p:txBody><a:bodyPr anchor="t"/><a:lstStyle/><a:p><a:r>'
      + '<a:rPr sz="1400" b="1"/><a:t>New</a:t></a:r></a:p></p:txBody>');
    expect(edit.result).toEqual({});
  });

  it('warns when the new runs drop the formatting the old ones had', () => {
    const plain = '<a:p><a:r><a:t>Plain</a:t></a:r></a:p>';
    expect(editSlideText(slidePackage(), '2', plain).result.warning).toMatch(/run formatting/);
    const unformatted = slidePackage({ shapes: TITLE.replace(/<a:rPr [bi]="1"\/>/g, '') });
    expect(editSlideText(unformatted, '2', plain).result).toEqual({});
  });

  it('gives a shape without a text body one', () => {
    const empty = slidePackage({ shapes: TITLE.replace(/<p:txBody>[\s\S]*<\/p:txBody>/, '') });
    expect(readSlideText(empty, '2')).toMatch(/^\(empty: the shape has no text body/);
    expect(part(editSlideText(empty, '2', '<a:p><a:r><a:t>Made</a:t></a:r></a:p>').base64, SLIDE))
      .toContain('<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Made</a:t></a:r></a:p></p:txBody>');
  });

  it('refuses what is not paragraphs, a DTD, and a group', () => {
    const grouped = slidePackage({ shapes: `<p:grpSp><p:nvGrpSpPr><p:cNvPr id="9" name="Group"/><p:cNvGrpSpPr/>`
      + `<p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>${TITLE}</p:grpSp>` });
    expect(readSlideText(grouped, '9')).toContain('--- child id="2" (Title) ---');
    expect(() => editSlideText(grouped, '9', '<a:p/>')).toThrow(/child: 2/);
    expect(() => editSlideText(slidePackage(), '2', '<a:r><a:t>x</a:t></a:r>')).toThrow(/<a:p> paragraphs/);
    expect(() => editSlideText(slidePackage(), '2', '<a:p>')).toThrow(/not well-formed/);
    expect(() => editSlideText(slidePackage(), '2', '<!DOCTYPE x [<!ENTITY e "x">]><a:p/>')).toThrow(/DTD/);
  });
});

describe('copying a picture between slides', () => {
  const source = slidePackage({ shapes: PICTURE, types: '<Default Extension="svg" ContentType="image/svg+xml"/>',
    rels: `<Relationship Id="rId2" Type="${RELS}/image" Target="../media/image1.png"/>`
      + `<Relationship Id="rId3" Type="${RELS}/image" Target="../media/image2.svg"/>`,
    parts: { 'ppt/media/image1.png': new Uint8Array([1, 2]), 'ppt/media/image2.svg': '<svg/>' } });

  it('brings its media, relationships and content types, and keeps its crop and alt text', () => {
    const edit = copyImage(source, slidePackage(), '7', {});
    expect(edit.result).toEqual({ shape_id: '3' });
    const parts = unpack(edit.base64);
    expect(parts['ppt/media/image1.png']).toEqual(new Uint8Array([1, 2]));
    expect(strFromU8(parts['ppt/media/image1.svg'])).toBe('<svg/>');
    const slide = strFromU8(parts[SLIDE]);
    expect(slide).toContain('<p:cNvPr id="3" name="Picture 3" descr="Brand logo"/>');
    expect(slide).toContain('<a:srcRect l="1000" r="2000"/>');
    const rels = strFromU8(parts['ppt/slides/_rels/slide1.xml.rels']);
    expect([...slide.matchAll(/r:embed="([^"]+)"/g)].map(match => rels.includes(`Id="${match[1]}"`))).toEqual([true, true]);
    expect(rels).toContain('Target="../media/image1.svg"');
    expect(strFromU8(parts['[Content_Types].xml'])).toContain('<Default Extension="svg" ContentType="image/svg+xml"/>');
  });

  it('places it where it is told, in points, without renaming the target\'s own media', () => {
    const target = slidePackage({ types: '<Default Extension="svg" ContentType="application/octet-stream"/>',
      parts: { 'ppt/media/image1.png': new Uint8Array([9]) } });
    const parts = unpack(copyImage(source, target, '7', { x: 10, width: 20 }).base64);
    expect(parts['ppt/media/image1.png']).toEqual(new Uint8Array([9]));
    expect(parts['ppt/media/image2.png']).toEqual(new Uint8Array([1, 2]));
    expect(strFromU8(parts['[Content_Types].xml']))
      .toContain('<Override PartName="/ppt/media/image1.svg" ContentType="image/svg+xml"/>');
    expect(strFromU8(parts[SLIDE])).toContain('<a:off x="127000" y="25400"/><a:ext cx="254000" cy="50800"/>');
  });

  it('brings every relationship the picture names, a hyperlink out of the deck too, and leaves its placeholder', () => {
    const clip = '<p:pic><p:nvPicPr><p:cNvPr id="8" name="Clip"><a:hlinkClick r:id="rId4"/></p:cNvPr><p:cNvPicPr/>'
      + '<p:nvPr><p:ph type="pic" idx="13"/><a:videoFile r:link="rId5"/><p:extLst><p:ext uri="media"><p14:media '
      + 'xmlns:p14="http://schemas.microsoft.com/office/powerpoint/2010/main" r:embed="rId6"/></p:ext></p:extLst>'
      + '</p:nvPr></p:nvPicPr><p:blipFill><a:blip r:embed="rId2"/></p:blipFill><p:spPr><a:xfrm><a:off x="0" y="0"/>'
      + '<a:ext cx="10" cy="10"/></a:xfrm></p:spPr></p:pic>';
    const video = slidePackage({ shapes: clip, types: '<Default Extension="mp4" ContentType="video/mp4"/>',
      rels: `<Relationship Id="rId2" Type="${RELS}/image" Target="../media/image1.png"/>`
        + `<Relationship Id="rId4" Type="${R}/hyperlink" Target="https://example.com/" TargetMode="External"/>`
        + `<Relationship Id="rId5" Type="${R}/video" Target="../media/media1.mp4"/>`
        + '<Relationship Id="rId6" Type="http://schemas.microsoft.com/office/2007/relationships/media" '
        + 'Target="../media/media1.mp4"/>',
      parts: { 'ppt/media/image1.png': new Uint8Array([1]), 'ppt/media/media1.mp4': new Uint8Array([2]) } });
    const parts = unpack(copyImage(video, slidePackage(), '8', {}).base64);
    const slide = strFromU8(parts[SLIDE]);
    const rels = new Map([...strFromU8(parts['ppt/slides/_rels/slide1.xml.rels']).matchAll(/<Relationship [^>]*\/>/g)]
      .map(([relationship]) => [/Id="([^"]+)"/.exec(relationship)![1], relationship]));
    const named = [...slide.matchAll(/r:(embed|link|id)="([^"]+)"/g)].map(match => rels.get(match[2]));
    expect(named).toEqual([expect.stringMatching(/hyperlink" Target="https:\/\/example.com\/" TargetMode="External"/),
      expect.stringContaining('Target="../media/media1.mp4"'), expect.stringContaining('Target="../media/media1.mp4"'),
      expect.stringContaining('Target="../media/image1.png"')]);
    expect(Object.keys(parts).filter(path => path.startsWith('ppt/media/')).sort())
      .toEqual(['ppt/media/image1.png', 'ppt/media/media1.mp4']);
    expect(strFromU8(parts['[Content_Types].xml'])).toContain('<Default Extension="mp4" ContentType="video/mp4"/>');
    expect(slide).not.toContain('<p:ph');
  });

  it('refuses a shape that is not a picture, one inside a group, and one that links a file outside the deck', () => {
    expect(() => copyImage(slidePackage(), slidePackage(), '2', {})).toThrow(/not a picture/);
    const grouped = slidePackage({ shapes: `<p:grpSp><p:nvGrpSpPr><p:cNvPr id="9" name="Group"/><p:cNvGrpSpPr/>`
      + `<p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>${PICTURE}</p:grpSp>` });
    expect(() => copyImage(grouped, slidePackage(), '7', {})).toThrow(/Picture 7 is inside a group/);
    const linked = slidePackage({ shapes: PICTURE.replace('r:embed="rId2"', 'r:embed="rId2" r:link="rId4"'),
      rels: `<Relationship Id="rId2" Type="${RELS}/image" Target="../media/image1.png"/>`
        + `<Relationship Id="rId4" Type="${R}/image" Target="https://example.com/a.png" TargetMode="External"/>`
        + `<Relationship Id="rId3" Type="${RELS}/image" Target="../media/image1.png"/>`,
      parts: { 'ppt/media/image1.png': new Uint8Array([1]) } });
    expect(() => copyImage(linked, slidePackage(), '7', {})).toThrow(/links https:\/\/example.com\/a.png, outside/);
  });
});

describe('charts on a slide', () => {
  it('adds the chart part, its relationship, content type and frame', () => {
    const edit = createChart(slidePackage(), { ...COLUMN, growth_arrows: [{ label: '+100%' }] });
    expect(edit.result).toEqual({ shape_ids: ['3', '4'], chart_part: 'ppt/charts/chart1.xml' });
    const parts = unpack(edit.base64);
    expect(strFromU8(parts['ppt/charts/chart1.xml'])).toContain('<c:v>20</c:v>');
    expect(strFromU8(parts['ppt/slides/_rels/slide1.xml.rels']))
      .toContain(`<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" `
        + 'Target="../charts/chart1.xml"/>');
    expect(strFromU8(parts['[Content_Types].xml'])).toContain('PartName="/ppt/charts/chart1.xml"');
    expect(strFromU8(parts[SLIDE])).toMatch(/<p:cNvPr id="3" name="Chart"\/>[\s\S]*r:id="rId2"[\s\S]*GrowthArrow f3 0/);
    expect(createChart(slidePackage(), { type: 'big_number', title: '7%', subtitle: 'growth' }).result)
      .toEqual({ shape_ids: ['3', '4'] });
  });

  it('fits a chart without a position to the slide, whatever its size', () => {
    const frame = (size: number[]) => /<p:xfrm>(.*?)<\/p:xfrm>/.exec(part(createChart(slidePackage({ size }), COLUMN)
      .base64, SLIDE))![1];
    expect(frame([12192000, 6858000])).toBe('<a:off x="457200" y="300000"/><a:ext cx="11277600" cy="5900000"/>');
    expect(frame([9144000, 6858000])).toBe('<a:off x="342900" y="300000"/><a:ext cx="8458200" cy="5900000"/>');
  });

  it('reads back the style it was built with', () => {
    const edit = createChart(slidePackage(), { ...COLUMN, accent_index: 1, style: { accent: '#C00000', font: 'Arial' },
      construction: { legend: 'bottom', value_axis: 'shown', gridlines: true } });
    expect(readChartStyle(edit.base64)).toEqual([{ chart_part: 'ppt/charts/chart1.xml', style: expect.objectContaining({
      form: 'column', series_count: 1, fonts: ['Arial'], legend: 'bottom', value_axis: 'shown', gridlines: true,
      has_native_title: false, series_fills: ['BFBFBF', 'C00000'], series_color_pattern: 'one_accent', gap_width: 50,
      unsupported: [] }) }]);
  });

  it('draws the chart index names again in its frame, and only its own growth arrows', () => {
    const spec = { ...COLUMN, growth_arrows: [{ from: 0, to: 1, label: '+100%' }] };
    const two = createChart(createChart(slidePackage(), spec).base64, spec).base64;
    const before = unpack(two);
    const edit = editBuiltChart(two, { ...COLUMN, values: [70, 80] }, 1);
    expect(edit.result).toEqual({ chart_part: 'ppt/charts/chart2.xml' });
    const after = unpack(edit.base64);
    expect(after['ppt/charts/chart1.xml']).toEqual(before['ppt/charts/chart1.xml']);
    expect(strFromU8(after['ppt/charts/chart2.xml'])).toContain('<c:v>80</c:v>');
    expect(after['ppt/slides/_rels/slide1.xml.rels']).toEqual(before['ppt/slides/_rels/slide1.xml.rels']);
    const slide = strFromU8(after[SLIDE]);
    expect(slide).toContain('name="GrowthArrow f3 0"');
    expect(slide).not.toContain('GrowthArrow f7');
    expect(slide).toContain('<p:cNvPr id="7" name="Chart"/>');
  });

  it('counts charts by the number of their part, not by frame order', () => {
    const two = unpack(createChart(createChart(slidePackage(), COLUMN).base64, COLUMN).base64);
    const renumbered = pack({ ...two, 'ppt/charts/chart10.xml': two['ppt/charts/chart1.xml'],
      'ppt/charts/chart1.xml': undefined as never,
      'ppt/slides/_rels/slide1.xml.rels': strFromU8(two['ppt/slides/_rels/slide1.xml.rels'])
        .replace('chart1.xml', 'chart10.xml') });
    expect(readChartStyle(renumbered).map(chart => chart.chart_part))
      .toEqual(['ppt/charts/chart2.xml', 'ppt/charts/chart10.xml']);
    expect(part(editBuiltChart(renumbered, { ...COLUMN, values: [7, 8] }, 1).base64, 'ppt/charts/chart10.xml'))
      .toContain('<c:v>8</c:v>');
  });

  it('refuses a chart the builder cannot draw again, a big number, an index past the charts, and a position', () => {
    const created = createChart(slidePackage(), COLUMN).base64;
    const pie = withParts(created, { 'ppt/charts/chart1.xml': part(created, 'ppt/charts/chart1.xml')
      .replace(/c:barChart/g, 'c:pieChart') });
    expect(() => editBuiltChart(pie, COLUMN, 0)).toThrow(/cannot draw this chart again \(pie chart\)/);
    expect(() => editBuiltChart(created, { type: 'big_number', title: '9', subtitle: 'x' }, 0)).toThrow(/big_number/);
    expect(() => editBuiltChart(created, COLUMN, 1)).toThrow(/chart_index 1 is past the slide's 1 charts/);
    expect(() => editBuiltChart(created, { ...COLUMN, position: { x: 0, y: 0, w: 914400, h: 914400 } }, 0))
      .toThrow(/keeps the chart where it is.*presentation_apply_patch update_shape/);
  });
});

describe('what a slide package says about the deck', () => {
  it('gives the theme palette and fonts and the master\'s body size', () => {
    const base64 = slidePackage({ parts: {
      'ppt/theme/theme1.xml': '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Brand">'
        + '<a:themeElements><a:clrScheme name="Brand"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
        + '<a:accent1><a:srgbClr val="c00000"/></a:accent1></a:clrScheme><a:fontScheme name="Brand"><a:majorFont>'
        + '<a:latin typeface="Georgia"/></a:majorFont><a:minorFont><a:latin typeface="Arial"/></a:minorFont>'
        + '</a:fontScheme></a:themeElements></a:theme>',
      'ppt/slideMasters/slideMaster1.xml': `<p:sldMaster ${NS}><p:txStyles><p:bodyStyle><a:lvl1pPr><a:defRPr sz="2000"/>`
        + '</a:lvl1pPr></p:bodyStyle></p:txStyles></p:sldMaster>',
    } });
    expect(themeOf(base64)).toEqual({ theme_palette: { dk1: '000000', accent1: 'C00000' },
      theme_fonts: { major: 'Georgia', minor: 'Arial' }, template_body_font_pt: 20 });
    expect(themeOf(slidePackage())).toEqual({ theme_palette: null, theme_fonts: null, template_body_font_pt: null });
  });

  const relationshipsOf = (entries: [id: string, type: string, target: string, external?: boolean][]) =>
    `<Relationships xmlns="${RELS}">${entries.map(([id, type, target, external]) => `<Relationship Id="${id}" `
      + `Type="${R}/${type}" Target="${target}"${external ? ' TargetMode="External"' : ''}/>`).join('')}</Relationships>`;
  /** A deck of two slides on one layout and master; ``rels`` are more relationships of the parts named. */
  const deck = (rels: Record<string, [string, string, string, boolean?][]> = {}) => pack({
    'ppt/presentation.xml': `<p:presentation ${NS}><p:sldIdLst><p:sldId id="256" r:id="rId2"/>`
      + '<p:sldId id="300" r:id="rId3"/></p:sldIdLst></p:presentation>',
    'ppt/_rels/presentation.xml.rels': relationshipsOf([['rId2', 'slide', 'slides/slide1.xml'],
      ['rId3', 'slide', 'slides/slide2.xml']]),
    ...Object.fromEntries([1, 2].map(slide => [`ppt/slides/_rels/slide${slide}.xml.rels`, relationshipsOf([['rId1',
      'slideLayout', '../slideLayouts/slideLayout1.xml'], ...rels[`slide${slide}`] ?? []])])),
    'ppt/slideLayouts/_rels/slideLayout1.xml.rels': relationshipsOf([['rId1', 'slideMaster',
      '../slideMasters/slideMaster1.xml'], ...rels.layout ?? []]),
    'ppt/slideMasters/_rels/slideMaster1.xml.rels': relationshipsOf(rels.master ?? []),
  });

  it('lists the slides to import with their ids as insertSlidesFromBase64 takes them', () => {
    expect(importedSlides(deck())).toEqual([{ position: 1, id: '256#' }, { position: 2, id: '300#' }]);
    expect(importedSlides(deck(), [2, 2])).toEqual([{ position: 2, id: '300#' }]);
    expect(() => importedSlides(deck(), [3])).toThrow('source_positions has 3, but the .pptx has 2 slides');
    expect(() => importedSlides(pack({ 'word/document.xml': '<x/>' }))).toThrow(/not a PowerPoint deck/);
  });

  it('refuses a slide to import, or its layout or master, that links outside the file or holds an object', () => {
    expect(importedSlides(deck({ slide1: [['rId9', 'hyperlink', 'https://example.com/', true]] }))).toHaveLength(2);
    const image = deck({ slide2: [['rId9', 'image', 'https://example.com/a.png', true]] });
    expect(() => importedSlides(image)).toThrow('Slide 2 of the .pptx links https://example.com/a.png outside the '
      + 'file, which import_slides does not bring in');
    expect(importedSlides(image, [1])).toHaveLength(1);
    expect(() => importedSlides(deck({ layout: [['rId9', 'oleObject', '../embeddings/oleObject1.bin']] })))
      .toThrow('ppt/slideLayouts/slideLayout1.xml, which slide 1 of the .pptx uses, holds an OLE object '
        + '(ppt/embeddings/oleObject1.bin)');
    expect(() => importedSlides(deck({ master: [['rId9', 'control', '../activeX/activeX1.xml']] })))
      .toThrow(/^ppt\/slideMasters\/slideMaster1.xml, which slide 1 of the .pptx uses, holds an ActiveX control/);
    expect(() => importedSlides(deck({ slide1: [['rId9', 'image', '../embeddings/preview.emf']] })))
      .toThrow(/^Slide 1 of the .pptx holds an embedded object/);
  });

});
