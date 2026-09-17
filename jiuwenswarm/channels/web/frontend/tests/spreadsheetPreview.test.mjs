import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ExcelJS from 'exceljs';

import {
  artifactBinaryPreviewUrl,
  artifactDownloadUrl,
  artifactTextPreviewUrl,
  previewKind,
} from '../node_modules/.cache/spreadsheet-preview/filePreviewModel.js';
import {
  MAX_SPREADSHEET_PREVIEW_BYTES,
  axisIndexAtOffset,
  buildAxisMetrics,
  columnLabel,
  parseCellRange,
} from '../node_modules/.cache/spreadsheet-preview/spreadsheetPreviewModel.js';
import {
  categoryCenter,
  categoryPoint,
  clusteredCategoryBand,
  ensureNonZeroAxisSpan,
  linearPosition,
  spanFromBaseline,
} from '../node_modules/.cache/spreadsheet-preview/chartGeometry.js';
import { officeFontStack, parseSpreadsheetWorkbook } from '../node_modules/.cache/spreadsheet-preview/spreadsheetWorkbookParser.js';

test('recognizes OOXML workbooks but leaves legacy XLS files unsupported', () => {
  assert.equal(previewKind({ name: 'report.xlsx' }), 'spreadsheet');
  assert.equal(previewKind({ name: 'REPORT.XLSX', mimeType: 'application/octet-stream' }), 'spreadsheet');
  assert.equal(previewKind({ name: 'automation.xlsm' }), 'spreadsheet');
  assert.equal(previewKind({ name: 'automation.bin', mimeType: 'application/vnd.ms-excel.sheet.macroEnabled.12' }), 'spreadsheet');
  assert.equal(
    previewKind({
      name: 'report.bin',
      mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    }),
    'spreadsheet',
  );
  assert.equal(previewKind({ name: 'legacy.xls' }), 'unsupported');
  assert.equal(previewKind({ name: 'data.csv' }), 'text');
});

test('previews only explicitly supported browser image formats', () => {
  const supportedExtensions = [
    'apng',
    'avif',
    'bmp',
    'cur',
    'gif',
    'ico',
    'jpe',
    'jfif',
    'jpeg',
    'jpg',
    'pjp',
    'pjpeg',
    'png',
    'svg',
    'webp',
  ];
  for (const extension of supportedExtensions) {
    assert.equal(previewKind({ name: `image.${extension}`, mimeType: 'application/octet-stream' }), 'image');
  }

  const supportedMimeTypes = [
    'image/apng',
    'image/avif',
    'image/bmp',
    'image/gif',
    'image/jpeg',
    'image/png',
    'image/svg+xml',
    'image/vnd.microsoft.icon',
    'image/webp',
    'image/x-icon',
  ];
  for (const mimeType of supportedMimeTypes) {
    assert.equal(previewKind({ name: 'image.bin', mimeType }), 'image');
  }

  assert.equal(previewKind({ name: 'image.tif', mimeType: 'image/tiff' }), 'unsupported');
  assert.equal(previewKind({ name: 'image.tiff', mimeType: 'image/tiff' }), 'unsupported');
  assert.equal(previewKind({ name: 'image.bin', mimeType: 'image/heic' }), 'unsupported');
});

test('uses signed download URLs without decoding them into local file paths', () => {
  const signedResource = {
    downloadUrl: '/file-api/download?token=signed-token',
    path: '/tmp/report.xlsx',
  };
  assert.equal(artifactDownloadUrl(signedResource), '/file-api/download?token=signed-token');
  assert.equal(artifactBinaryPreviewUrl(signedResource, 'https://example.test'), '/file-api/download?token=signed-token&inline=1');
  assert.equal(artifactTextPreviewUrl(signedResource, 'https://example.test'), '/file-api/download?token=signed-token&inline=1');

  const taskResource = { path: 'agent/workspace/report.xlsx' };
  assert.equal(artifactDownloadUrl(taskResource), '/file-api/raw-file?path=agent%2Fworkspace%2Freport.xlsx');
  assert.equal(artifactBinaryPreviewUrl(taskResource, 'https://example.test'), '/file-api/raw-file?path=agent%2Fworkspace%2Freport.xlsx');
  assert.equal(artifactTextPreviewUrl(taskResource, 'https://example.test'), '/file-api/file-content?path=agent%2Fworkspace%2Freport.xlsx&encoding=auto');
});

test('builds exact virtual-axis offsets including hidden rows', () => {
  const metrics = buildAxisMetrics(4, 20, [
    { index: 2, size: 36 },
    { index: 3, size: 0 },
  ]);
  assert.deepEqual(Array.from(metrics.offsets), [0, 20, 56, 56, 76]);
  assert.equal(metrics.totalSize, 76);
  assert.equal(axisIndexAtOffset(metrics.offsets, 0), 1);
  assert.equal(axisIndexAtOffset(metrics.offsets, 20), 2);
  assert.equal(axisIndexAtOffset(metrics.offsets, 55), 2);
  assert.equal(axisIndexAtOffset(metrics.offsets, 56), 4);
});

test('converts spreadsheet coordinates without truncating the Excel range', () => {
  assert.equal(columnLabel(1), 'A');
  assert.equal(columnLabel(26), 'Z');
  assert.equal(columnLabel(27), 'AA');
  assert.equal(columnLabel(16384), 'XFD');
  assert.deepEqual(parseCellRange('$B$2:$XFD$1048576'), {
    top: 2,
    left: 2,
    bottom: 1048576,
    right: 16384,
  });
  assert.equal(MAX_SPREADSHEET_PREVIEW_BYTES, 50 * 1024 * 1024);
});

test('maps signed chart values from the zero baseline on either axis', () => {
  const horizontalBaseline = linearPosition(0, -40, 100, 0, 140);
  assert.deepEqual(spanFromBaseline(linearPosition(100, -40, 100, 0, 140), horizontalBaseline), { start: 40, size: 100 });
  assert.deepEqual(spanFromBaseline(linearPosition(10, -40, 100, 0, 140), horizontalBaseline), { start: 40, size: 10 });
  assert.deepEqual(spanFromBaseline(linearPosition(-40, -40, 100, 0, 140), horizontalBaseline), { start: 0, size: 40 });

  const verticalBaseline = linearPosition(0, -40, 100, 280, 0);
  const positiveColumn = spanFromBaseline(linearPosition(100, -40, 100, 280, 0), verticalBaseline);
  const negativeColumn = spanFromBaseline(linearPosition(-40, -40, 100, 280, 0), verticalBaseline);
  assert.deepEqual(positiveColumn, { start: 0, size: 200 });
  assert.deepEqual(negativeColumn, { start: 200, size: 80 });
});

test('centres categories and allocates non-overlapping clustered bands', () => {
  assert.equal(categoryCenter(1, 4, 0, 400), 150);
  assert.equal(categoryPoint(0, 4, 0, 400), 0);
  assert.equal(categoryPoint(3, 4, 0, 400), 400);
  assert.equal(categoryPoint(0, 1, 0, 400), 200);
  assert.deepEqual(clusteredCategoryBand(1, 4, 0, 2, 0, 400), { start: 114, size: 36 });
  assert.deepEqual(clusteredCategoryBand(1, 4, 1, 2, 0, 400), { start: 150, size: 36 });
});

test('keeps zero as the upper boundary for an all-negative chart', () => {
  assert.deepEqual(ensureNonZeroAxisSpan(-40, 0, 10), { minimum: -40, maximum: 0 });
  assert.deepEqual(ensureNonZeroAxisSpan(0, 0, 10), { minimum: 0, maximum: 10 });
});

test('parses workbook sheets, formatting, merges and cached formula results', async () => {
  const source = new ExcelJS.Workbook();
  source.views = [{ activeTab: 1 }];
  const summary = source.addWorksheet('汇总');
  const details = source.addWorksheet('Details');

  summary.getCell('A1').value = 'Revenue';
  summary.getCell('A1').font = { bold: true, size: 21, color: { argb: 'FFFFFFFF' } };
  summary.getCell('A1').fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FF1F4E78' } };
  summary.getCell('B2').value = 0.125;
  summary.getCell('B2').numFmt = '0.00%';
  summary.getCell('C3').value = new Date('2024-01-01T00:00:00.000Z');
  summary.getCell('C3').numFmt = 'yyyy-mm-dd';
  summary.getCell('D4').value = { formula: 'SUM(1,2)', result: 3 };
  summary.mergeCells('A5:B6');
  summary.getCell('A5').value = 'Merged';
  summary.getCell('A1').note = 'Source-controlled note';
  summary.autoFilter = 'A1:B2';
  summary.getRow(2).height = 30;
  summary.getColumn(2).width = 18;
  details.getCell('A1').value = 'Second sheet';
  details.getCell('A2').value = 'Theme colors';
  details.getCell('A2').font = { color: { theme: 1 } };
  details.getCell('A2').fill = { type: 'pattern', pattern: 'solid', fgColor: { theme: 0 } };

  const buffer = await source.xlsx.writeBuffer();
  const parsed = await parseSpreadsheetWorkbook(buffer);

  assert.deepEqual(
    parsed.sheets.map(sheet => sheet.name),
    ['汇总', 'Details'],
  );
  assert.equal(parsed.activeSheetIndex, 1);
  assert.deepEqual(parsed.sheets[0].merges, [{ top: 5, left: 1, bottom: 6, right: 2 }]);
  assert.equal(parsed.sheets[0].rowCount, 6);
  assert.equal(parsed.sheets[0].columnCount, 4);
  assert.deepEqual(parsed.sheets[0].autoFilter, { top: 1, left: 1, bottom: 2, right: 2 });
  assert.deepEqual(parsed.sheets[0].comments, [{ row: 1, column: 1, comment: { author: 'Author', text: 'Source-controlled note' } }]);
  assert.ok(parsed.sheets[0].rowSizes.some(item => item.index === 2 && item.size === 40));
  assert.ok(parsed.sheets[0].columnSizes.some(item => item.index === 2 && item.size > 100));

  const cells = new Map(parsed.sheets[0].rows.flatMap(row => row.cells.map(cell => [`${cell.row}:${cell.column}`, cell])));
  assert.equal(cells.get('2:2').text, '12.50%');
  assert.equal(cells.get('3:3').text, '2024-01-01');
  assert.equal(cells.get('4:4').text, '3');
  assert.equal(cells.get('4:4').formula, 'SUM(1,2)');
  assert.equal(cells.get('4:4').alignRight, true);

  const titleStyle = parsed.styles[cells.get('1:1').styleId];
  assert.equal(titleStyle.fontWeight, 700);
  assert.equal(titleStyle.lineHeight, 'normal');
  assert.equal(titleStyle.color, '#FFFFFF');
  assert.equal(titleStyle.backgroundColor, '#1F4E78');
  const themeCell = parsed.sheets[1].rows.flatMap(row => row.cells).find(cell => cell.row === 2 && cell.column === 1);
  const themeStyle = parsed.styles[themeCell.styleId];
  assert.equal(themeStyle.color, '#000000');
  assert.equal(themeStyle.backgroundColor, '#FFFFFF');
});

test('uses the shared Office font stack for unavailable workbook fonts', async () => {
  assert.equal(
    officeFontStack('Aptos', '宋体'),
    '"Aptos", "Microsoft YaHei", "宋体", "PingFang SC", "HarmonyOS Sans SC", "HarmonyOS Sans", "Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", "WenQuanYi Micro Hei", sans-serif',
  );
  assert.equal(
    officeFontStack('Aptos', '游ゴシック'),
    '"Aptos", "游ゴシック", "Microsoft YaHei", "PingFang SC", "HarmonyOS Sans SC", "HarmonyOS Sans", "Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", "WenQuanYi Micro Hei", "SimSun", sans-serif',
  );

  const source = new ExcelJS.Workbook();
  const sheet = source.addWorksheet('Fonts');
  sheet.getCell('A1').value = 'Preview text';
  sheet.getCell('A1').font = { name: 'NotInstalledFontPro-Bold' };

  const parsed = await parseSpreadsheetWorkbook(await source.xlsx.writeBuffer());
  const cell = parsed.sheets[0].rows[0].cells[0];
  assert.equal(
    parsed.styles[cell.styleId].fontFamily,
    '"NotInstalledFontPro-Bold", "Microsoft YaHei", "PingFang SC", "HarmonyOS Sans SC", "HarmonyOS Sans", "Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", "WenQuanYi Micro Hei", "SimSun", sans-serif',
  );
});

test('keeps a formula visible when the workbook has no cached result', async () => {
  const source = new ExcelJS.Workbook();
  const sheet = source.addWorksheet('Formula');
  sheet.getCell('A1').value = { formula: 'NOW()' };
  const parsed = await parseSpreadsheetWorkbook(await source.xlsx.writeBuffer());
  assert.equal(parsed.sheets[0].rows[0].cells[0].text, '=NOW()');
});

test('applies common conditional-formatting rules without recalculating formulas', async () => {
  const source = new ExcelJS.Workbook();
  const sheet = source.addWorksheet('Conditional formatting');
  sheet.getCell('A1').value = 4;
  sheet.getCell('A2').value = 9;
  sheet.addConditionalFormatting({
    ref: 'A1:A2',
    rules: [
      {
        type: 'cellIs',
        operator: 'greaterThan',
        formulae: ['5'],
        style: { fill: { type: 'pattern', pattern: 'solid', fgColor: { argb: '00F4CCCC' } } },
      },
    ],
  });
  const parsed = await parseSpreadsheetWorkbook(await source.xlsx.writeBuffer());
  const highCell = parsed.sheets[0].rows.flatMap(row => row.cells).find(cell => cell.row === 2 && cell.column === 1);
  assert.equal(parsed.styles[highCell.styleId].backgroundColor, '#F4CCCC');
});

test('preserves opaque OOXML colours and embedded images', async () => {
  const source = new ExcelJS.Workbook();
  const sheet = source.addWorksheet('Assets');
  sheet.getCell('A1').value = 'Brand';
  sheet.getCell('A1').fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: '0000AEEF' } };
  const imageId = source.addImage({
    base64: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL6YQAAAABJRU5ErkJggg==',
    extension: 'png',
  });
  sheet.addImage(imageId, 'B2:C4');

  const parsed = await parseSpreadsheetWorkbook(await source.xlsx.writeBuffer());
  assert.equal(parsed.styles[parsed.sheets[0].rows[0].cells[0].styleId].backgroundColor, '#00AEEF');
  assert.equal(parsed.sheets[0].images.length, 1);
  assert.match(parsed.sheets[0].images[0].source, /^data:image\/png;base64,/);
  assert.equal(parsed.sheets[0].images[0].anchor.fromColumn, 2);
  assert.equal(parsed.sheets[0].images[0].anchor.fromRow, 2);
});

test('keeps spreadsheet preview messages complete in Chinese and English', async () => {
  const localeNames = ['zh', 'en'];
  const keys = [
    'spreadsheetPreviewFailed',
    'spreadsheetResourceLimitExceeded',
    'spreadsheetTooLarge',
    'spreadsheetEmptyWorkbook',
    'spreadsheetEmptySheet',
    'spreadsheetSheetTabsLabel',
    'spreadsheetTableLabel',
  ];

  for (const localeName of localeNames) {
    const locale = JSON.parse(await readFile(new URL(`../src/i18n/locales/${localeName}.json`, import.meta.url), 'utf8'));
    keys.forEach(key => assert.equal(typeof locale.artifacts[key], 'string', `${localeName} is missing artifacts.${key}`));
    assert.match(locale.artifacts.spreadsheetTooLarge, /\{\{size\}\}/);
    assert.match(locale.artifacts.spreadsheetTableLabel, /\{\{sheet\}\}/);
  }
});
