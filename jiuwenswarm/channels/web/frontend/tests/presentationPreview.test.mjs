import assert from 'node:assert/strict';
import test from 'node:test';
import JSZip from 'jszip';
import en from '../src/i18n/locales/en.json' with { type: 'json' };
import zh from '../src/i18n/locales/zh.json' with { type: 'json' };
import { previewKind } from '../node_modules/.cache/presentation-preview/filePreviewModel.js';
import {
  OoxmlArchiveLimitError,
  PRESENTATION_ARCHIVE_LIMITS,
  SPREADSHEET_ARCHIVE_LIMITS,
  inspectOoxmlArchive,
} from '../node_modules/.cache/presentation-preview/ooxmlArchiveLimits.js';
import { parsePresentation } from '../node_modules/.cache/presentation-preview/pptxPresentationParser.js';
import { presentationLineHeight } from '../node_modules/.cache/presentation-preview/pptxPreviewModel.js';

function assertClose(actual, expected) {
  assert.ok(Math.abs(actual - expected) < 1e-9, `expected ${actual} to be close to ${expected}`);
}

function presentationArchive(slides, additionalFiles = {}) {
  const archive = new JSZip();
  const slideIds = slides.map((_, index) => `<p:sldId id="${256 + index}" r:id="rId${index + 1}"/>`).join('');
  const relationships = slides
    .map(
      (_, index) =>
        `<Relationship Id="rId${index + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide${index + 1}.xml"/>`,
    )
    .join('');

  archive.file(
    'ppt/presentation.xml',
    `<p:presentation xmlns:p="p" xmlns:r="r"><p:sldSz cx="9144000" cy="6858000"/><p:sldIdLst>${slideIds}</p:sldIdLst></p:presentation>`,
  );
  archive.file('ppt/_rels/presentation.xml.rels', `<Relationships xmlns="r">${relationships}</Relationships>`);
  slides.forEach((slide, index) => archive.file(`ppt/slides/slide${index + 1}.xml`, slide));
  Object.entries(additionalFiles).forEach(([path, contents]) => archive.file(path, contents));
  return archive;
}

test('recognizes read-only OOXML presentation formats', () => {
  assert.equal(previewKind({ name: 'proposal.pptx' }), 'presentation');
  assert.equal(previewKind({ name: 'board-deck.pptm' }), 'presentation');
  assert.equal(previewKind({ name: 'legacy.ppt' }), 'unsupported');
  assert.equal(previewKind({ name: 'deck.bin', mimeType: 'application/vnd.openxmlformats-officedocument.presentationml.presentation' }), 'presentation');
});

test('uses font metrics when DrawingML omits explicit line spacing', () => {
  assert.equal(presentationLineHeight(undefined), 'normal');
  assert.equal(presentationLineHeight({ kind: 'relative', value: 1.2 }), 1.2);
  assert.equal(presentationLineHeight({ kind: 'absolute', value: 24 }), '24px');
});

test('keeps presentation preview messages complete in Chinese and English', () => {
  const keys = [
    'presentationPreviewFailed',
    'presentationResourceLimitExceeded',
    'presentationTooLarge',
    'presentationSlides',
    'presentationPage',
    'presentationSlideIncomplete',
    'presentationStructureInvalid',
    'presentationGoToSlide',
    'presentationPreviousSlide',
    'presentationNextSlide',
    'presentationZoomOut',
    'presentationZoomIn',
    'presentationZoomLevel',
  ];
  for (const locale of [en, zh]) keys.forEach(key => assert.equal(typeof locale.artifacts[key], 'string', `${key} is missing`));
  assert.match(en.artifacts.presentationTooLarge, /\{\{size\}\}/);
  assert.match(zh.artifacts.presentationPage, /\{\{current\}\}/);
  assert.match(zh.artifacts.presentationPage, /\{\{total\}\}/);
});

test('enforces presentation and spreadsheet archive limits before decompression', () => {
  assert.equal(PRESENTATION_ARCHIVE_LIMITS.maxCompressedBytes, 50 * 1024 * 1024);
  assert.equal(PRESENTATION_ARCHIVE_LIMITS.maxEntries, 5_000);
  assert.equal(PRESENTATION_ARCHIVE_LIMITS.maxEntryUncompressedBytes, 64 * 1024 * 1024);
  assert.equal(PRESENTATION_ARCHIVE_LIMITS.maxTotalUncompressedBytes, 200 * 1024 * 1024);
  assert.equal(SPREADSHEET_ARCHIVE_LIMITS.maxTotalUncompressedBytes, 128 * 1024 * 1024);

  assert.throws(
    () => inspectOoxmlArchive(new ArrayBuffer(11), { ...PRESENTATION_ARCHIVE_LIMITS, maxCompressedBytes: 10 }),
    error => error instanceof OoxmlArchiveLimitError && error.limit === 'compressed-size',
  );
  assert.throws(
    () => inspectOoxmlArchive(declaredArchive([64 * 1024 * 1024 + 1]), PRESENTATION_ARCHIVE_LIMITS),
    error => error instanceof OoxmlArchiveLimitError && error.limit === 'entry-size',
  );
  assert.doesNotThrow(() => inspectOoxmlArchive(declaredArchive([64, 64].map(size => size * 1024 * 1024)), SPREADSHEET_ARCHIVE_LIMITS));
  assert.throws(
    () => inspectOoxmlArchive(declaredArchive([50, 50, 50].map(size => size * 1024 * 1024)), SPREADSHEET_ARCHIVE_LIMITS),
    error => error instanceof OoxmlArchiveLimitError && error.limit === 'uncompressed-size',
  );
  assert.doesNotThrow(() => inspectOoxmlArchive(declaredArchive([50, 50, 50, 50].map(size => size * 1024 * 1024)), PRESENTATION_ARCHIVE_LIMITS));
  assert.throws(
    () => inspectOoxmlArchive(declaredArchive([51, 50, 50, 50].map(size => size * 1024 * 1024)), PRESENTATION_ARCHIVE_LIMITS),
    error => error instanceof OoxmlArchiveLimitError && error.limit === 'uncompressed-size',
  );
  assert.throws(
    () => inspectOoxmlArchive(declaredArchive([], 5_001), PRESENTATION_ARCHIVE_LIMITS),
    error => error instanceof OoxmlArchiveLimitError && error.limit === 'entry-count',
  );

  const zip64Archive = declaredZip64Archive(32 * 1024 * 1024);
  const wrappedArchive = new Uint8Array(zip64Archive.byteLength + 8);
  wrappedArchive.set(new Uint8Array(zip64Archive), 4);
  assert.doesNotThrow(() => inspectOoxmlArchive(wrappedArchive.subarray(4, -4), PRESENTATION_ARCHIVE_LIMITS));
});

function declaredArchive(uncompressedSizes, claimedEntryCount = uncompressedSizes.length) {
  const entryLength = 47;
  const directorySize = entryLength * uncompressedSizes.length;
  const buffer = new ArrayBuffer(directorySize + 22);
  const view = new DataView(buffer);
  uncompressedSizes.forEach((uncompressedSize, index) => {
    const offset = index * entryLength;
    view.setUint32(offset, 0x02014b50, true);
    view.setUint32(offset + 24, uncompressedSize, true);
    view.setUint16(offset + 28, 1, true);
    new Uint8Array(buffer, offset + 46, 1)[0] = 65 + (index % 26);
  });
  view.setUint32(directorySize, 0x06054b50, true);
  view.setUint16(directorySize + 8, claimedEntryCount, true);
  view.setUint16(directorySize + 10, claimedEntryCount, true);
  view.setUint32(directorySize + 12, directorySize, true);
  return buffer;
}

function declaredZip64Archive(uncompressedSize) {
  const directorySize = 67;
  const zip64EndOffset = directorySize;
  const locatorOffset = zip64EndOffset + 56;
  const endOffset = locatorOffset + 20;
  const buffer = new ArrayBuffer(endOffset + 22);
  const view = new DataView(buffer);

  view.setUint32(0, 0x02014b50, true);
  view.setUint32(20, 0xffffffff, true);
  view.setUint32(24, 0xffffffff, true);
  view.setUint16(28, 1, true);
  view.setUint16(30, 20, true);
  new Uint8Array(buffer, 46, 1)[0] = 65;
  view.setUint16(47, 0x0001, true);
  view.setUint16(49, 16, true);
  view.setBigUint64(51, BigInt(uncompressedSize), true);
  view.setBigUint64(59, 0n, true);

  view.setUint32(zip64EndOffset, 0x06064b50, true);
  view.setBigUint64(zip64EndOffset + 4, 44n, true);
  view.setBigUint64(zip64EndOffset + 24, 1n, true);
  view.setBigUint64(zip64EndOffset + 32, 1n, true);
  view.setBigUint64(zip64EndOffset + 40, BigInt(directorySize), true);

  view.setUint32(locatorOffset, 0x07064b50, true);
  view.setBigUint64(locatorOffset + 8, BigInt(zip64EndOffset), true);
  view.setUint32(locatorOffset + 16, 1, true);

  view.setUint32(endOffset, 0x06054b50, true);
  view.setUint16(endOffset + 8, 0xffff, true);
  view.setUint16(endOffset + 10, 0xffff, true);
  view.setUint32(endOffset + 12, 0xffffffff, true);
  view.setUint32(endOffset + 16, 0xffffffff, true);
  return buffer;
}

test('parses an OOXML slide into a stable browser presentation model', async () => {
  const archive = presentationArchive([
    `<?xml version="1.0" encoding="UTF-8"?>
    <p:sld xmlns:p="p" xmlns:a="a"><p:cSld name="Overview"><p:bg><p:bgPr><a:solidFill><a:srgbClr val="123456"/></a:solidFill></p:bgPr></p:bg><p:spTree>
      <p:nvGrpSpPr/><p:grpSpPr/>
      <p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr><a:xfrm><a:off x="914400" y="457200"/><a:ext cx="7315200" cy="914400"/></a:xfrm><a:prstGeom prst="roundRect"><a:avLst><a:gd name="adj" fmla="val 30000"/></a:avLst></a:prstGeom><a:solidFill><a:srgbClr val="123456"/></a:solidFill></p:spPr>
        <p:txBody><a:bodyPr><a:normAutofit/></a:bodyPr><a:lstStyle/><a:p><a:pPr algn="ctr" marL="342900" indent="-285750"><a:lnSpc><a:spcPct val="120000"/></a:lnSpc><a:spcBef><a:spcPts val="600"/></a:spcBef><a:defRPr sz="2400" spc="125" kern="1200" b="1"><a:solidFill><a:srgbClr val="ABCDEF"/></a:solidFill></a:defRPr></a:pPr><a:r><a:t>Quarterly review &amp; design &lt; 1%</a:t></a:r></a:p></p:txBody>
      </p:sp>
    </p:spTree></p:cSld></p:sld>`,
  ]);
  const presentation = await parsePresentation(await archive.generateAsync({ type: 'arraybuffer' }));
  assert.equal(presentation.width, 960);
  assert.equal(presentation.height, 720);
  assert.equal(presentation.slides.length, 1);
  assert.equal(presentation.slides[0].name, 'Overview');
  assert.deepEqual(presentation.slides[0].background, { kind: 'solid', color: '#123456', transparency: undefined });
  assert.equal(presentation.slides[0].nodes.length, 1);
  const shape = presentation.slides[0].nodes[0];
  assert.equal(shape.type, 'shape');
  assert.equal(shape.geometry, 'roundRect');
  assert.deepEqual(shape.adjustments, { adj: 30000 });
  assert.equal(shape.fill.color, '#123456');
  assert.equal(shape.text.autoFit, 'shrink');
  assert.equal(shape.text.paragraphs[0].runs[0].text, 'Quarterly review & design < 1%');
  assert.equal(shape.text.paragraphs[0].runs[0].fontSize, 32);
  assertClose(shape.text.paragraphs[0].runs[0].characterSpacing, 5 / 3);
  assert.equal(shape.text.paragraphs[0].runs[0].kerningThreshold, 16);
  assert.equal(shape.text.paragraphs[0].runs[0].color, '#ABCDEF');
  assert.equal(shape.text.paragraphs[0].marginLeft, 36);
  assert.equal(shape.text.paragraphs[0].indent, -30);
  assert.deepEqual(shape.text.paragraphs[0].spaceBefore, { kind: 'absolute', value: 8 });
  assert.deepEqual(shape.text.paragraphs[0].lineSpacing, { kind: 'relative', value: 1.2 });
});

test('applies stored normal-autofit font and line-spacing scales', async () => {
  const archive = presentationArchive([
    `<p:sld xmlns:p="p" xmlns:a="a"><p:cSld><p:spTree>
    <p:nvGrpSpPr/><p:grpSpPr/>
    <p:sp><p:nvSpPr><p:cNvPr id="2" name="Autofit"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
      <p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="9144000" cy="914400"/></a:xfrm><a:prstGeom prst="rect"/></p:spPr>
      <p:txBody><a:bodyPr><a:normAutofit fontScale="75000" lnSpcReduction="20000"/></a:bodyPr><a:lstStyle/><a:p>
        <a:pPr><a:lnSpc><a:spcPct val="120000"/></a:lnSpc><a:defRPr sz="2400"/></a:pPr><a:r><a:t>Scaled text</a:t></a:r>
      </a:p></p:txBody>
    </p:sp>
  </p:spTree></p:cSld></p:sld>`,
  ]);
  const presentation = await parsePresentation(await archive.generateAsync({ type: 'arraybuffer' }));
  const text = presentation.slides[0].nodes[0].text;
  assert.equal(text.autoFit, 'shrink');
  assert.equal(text.paragraphs[0].runs[0].fontSize, 24);
  assertClose(text.paragraphs[0].lineSpacing.value, 0.96);
});

test('parses table cell text and built-in row banding from standard OOXML', async () => {
  const archive = presentationArchive(
    [
      `<p:sld xmlns:p="p" xmlns:a="a"><p:cSld name="Weather"><p:spTree>
    <p:nvGrpSpPr/><p:grpSpPr/>
    <p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="2" name="Daily forecast"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>
      <p:xfrm><a:off x="0" y="0"/><a:ext cx="9144000" cy="914400"/></p:xfrm>
      <a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl>
        <a:tblPr firstRow="1" bandRow="1"><a:tableStyleId>{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}</a:tableStyleId></a:tblPr><a:tblGrid><a:gridCol w="9144000"/></a:tblGrid><a:tr h="914400"><a:tc>
          <a:txBody><a:bodyPr lIns="45720" rIns="45720" tIns="22860" bIns="22860"><a:spAutoFit/></a:bodyPr><a:lstStyle/>
            <a:p><a:pPr><a:defRPr sz="1400" lang="zh-CN" b="1"><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:defRPr></a:pPr><a:r><a:t>日期</a:t></a:r></a:p>
          </a:txBody><a:tcPr><a:solidFill><a:srgbClr val="2196F3"/></a:solidFill></a:tcPr>
        </a:tc></a:tr><a:tr h="914400"><a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>7月23日</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc></a:tr><a:tr h="914400"><a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>7月24日</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc></a:tr></a:tbl></a:graphicData></a:graphic>
    </p:graphicFrame>
  </p:spTree></p:cSld></p:sld>`,
    ],
    {
      'ppt/slides/_rels/slide1.xml.rels':
        '<Relationships xmlns="r"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>',
      'ppt/slideLayouts/slideLayout1.xml': '<p:sldLayout xmlns:p="p"><p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/></p:spTree></p:cSld></p:sldLayout>',
      'ppt/slideLayouts/_rels/slideLayout1.xml.rels':
        '<Relationships xmlns="r"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>',
      'ppt/slideMasters/slideMaster1.xml': '<p:sldMaster xmlns:p="p"><p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/></p:spTree></p:cSld></p:sldMaster>',
      'ppt/slideMasters/_rels/slideMaster1.xml.rels':
        '<Relationships xmlns="r"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/></Relationships>',
      'ppt/theme/theme1.xml':
        '<a:theme xmlns:a="a"><a:themeElements><a:clrScheme name="Test"><a:accent1><a:srgbClr val="4F81BD"/></a:accent1></a:clrScheme><a:fontScheme name="Test"><a:majorFont><a:latin typeface="Aptos Display"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Aptos"/><a:ea typeface=""/><a:cs typeface=""/><a:font script="Hans" typeface="宋体"/></a:minorFont></a:fontScheme></a:themeElements></a:theme>',
    },
  );
  const presentation = await parsePresentation(await archive.generateAsync({ type: 'arraybuffer' }));
  const table = presentation.slides[0].nodes[0];
  assert.equal(table.type, 'table');
  const text = table.rows[0].cells[0].text;
  assert.equal(text.paragraphs[0].runs[0].text, '日期');
  assertClose(text.paragraphs[0].runs[0].fontSize, 56 / 3);
  assert.equal(text.paragraphs[0].runs[0].fontFamily, 'Aptos');
  assert.equal(text.paragraphs[0].runs[0].eastAsianFontFamily, '宋体');
  assert.equal(text.paragraphs[0].runs[0].color, '#FFFFFF');
  assert.equal(text.autoFit, 'resize');
  assert.deepEqual(text.margin, { left: 4.8, right: 4.8, top: 2.4, bottom: 2.4 });
  assert.equal(table.rows[1].cells[0].fill.color, '#D0D8E8');
  assert.equal(table.rows[2].cells[0].fill.color, '#E9EDF4');
});

test('renders valid slides and rejects invalid slide XML without repairing it', async () => {
  const archive = presentationArchive([
    `<p:sld xmlns:p="p" xmlns:a="a"><p:cSld name="Valid"><p:spTree>
    <p:nvGrpSpPr/><p:grpSpPr/>
    <p:sp><p:nvSpPr><p:cNvPr id="2" name="Visible"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="914400" cy="914400"/></a:xfrm><a:prstGeom prst="rect"/><a:solidFill><a:srgbClr val="123456"/></a:solidFill></p:spPr></p:sp>
  </p:spTree></p:cSld></p:sld>`,
    `<p:sld xmlns:p="p" xmlns:a="a"><p:cSld name="Invalid"><p:spTree>
    <p:nvGrpSpPr/><p:grpSpPr/>
    <p:sp><p:nvSpPr><p:cNvPr id="2" name="Must not render"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="914400" cy="914400"/></a:xfrm><a:prstGeom prst="rect"/></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Research & Development</a:t></a:r></a:p></p:txBody></p:sp>
  </p:spTree></p:cSld></p:sld>`,
  ]);
  const presentation = await parsePresentation(await archive.generateAsync({ type: 'arraybuffer' }));
  assert.equal(presentation.slides.length, 2);
  assert.equal(presentation.slides[0].status, undefined);
  assert.equal(presentation.slides[0].nodes.length, 1);
  assert.equal(presentation.slides[0].nodes[0].name, 'Visible');
  assert.equal(presentation.slides[1].status, 'invalid');
  assert.equal(presentation.slides[1].nodes.length, 0);
});

test('leaves an unreadable slide blank instead of rendering a partial slide', async () => {
  const archive = presentationArchive([
    `<p:sld xmlns:p="p" xmlns:a="a"><p:cSld name="Unreadable"><p:spTree>
    <p:nvGrpSpPr/><p:grpSpPr/>
    <p:sp><p:nvSpPr><p:cNvPr id="2" name="Visible"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="914400" cy="914400"/></a:xfrm><a:prstGeom prst="rect"/></p:spPr></p:sp>
    <p:sp><p:nvSpPr><p:cNvPr id="3" name="Broken"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="914400" y="0"/><a:ext cx="914400" cy="914400"/></a:xfrm><a:prstGeom prst="rect"/></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>1 < 2</a:t></a:r></a:p></p:txBody></p:sp>
  </p:spTree></p:cSld></p:sld>`,
  ]);
  const presentation = await parsePresentation(await archive.generateAsync({ type: 'arraybuffer' }));
  assert.equal(presentation.slides.length, 1);
  assert.equal(presentation.slides[0].status, 'invalid');
  assert.equal(presentation.slides[0].nodes.length, 0);
});

test('keeps unsupported objects out of the slide canvas and reports the page in the header', async () => {
  const archive = presentationArchive([
    `<p:sld xmlns:p="p" xmlns:a="a"><p:cSld name="Mixed"><p:spTree>
    <p:nvGrpSpPr/><p:grpSpPr/>
    <p:sp><p:nvSpPr><p:cNvPr id="2" name="Visible"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="914400" cy="914400"/></a:xfrm><a:prstGeom prst="rect"/></p:spPr></p:sp>
    <p:sp><p:nvSpPr><p:cNvPr id="3" name="Unsupported"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="914400" y="0"/><a:ext cx="914400" cy="914400"/></a:xfrm><a:custGeom/></p:spPr></p:sp>
  </p:spTree></p:cSld></p:sld>`,
  ]);
  const presentation = await parsePresentation(await archive.generateAsync({ type: 'arraybuffer' }));
  assert.equal(presentation.slides[0].status, 'incomplete');
  assert.equal(presentation.slides[0].nodes.length, 1);
  assert.equal(presentation.slides[0].nodes[0].name, 'Visible');
});
