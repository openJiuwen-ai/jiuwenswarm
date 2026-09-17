import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import { FileIcon, getFileExtensionLabel, resolveFileIconType } from '../node_modules/.cache/file-icon/index.mjs';

const CASES = [
  ['report.pdf', 'pdf'],
  ['slides.pptx', 'ppt'],
  ['proposal.docx', 'word'],
  ['metrics.xlsx', 'xls'],
  ['sample.html', 'html'],
  ['diagram.svg', 'image'],
  ['recording.mp3', 'audio'],
  ['demo.mp4', 'video'],
  ['source.py', 'code'],
  ['settings.yaml', 'code'],
  ['notes.md', 'document'],
];

test('resolves each supported file category from the filename', () => {
  for (const [fileName, expected] of CASES) {
    assert.equal(resolveFileIconType(fileName), expected, fileName);
  }
});

test('matches extensions case-insensitively', () => {
  assert.equal(resolveFileIconType('REPORT.PDF'), 'pdf');
  assert.equal(resolveFileIconType('Preview.HtMl'), 'html');
});

test('uses the final extension of multi-dot names', () => {
  assert.equal(resolveFileIconType('report.final.docx'), 'word');
  assert.equal(resolveFileIconType('report.pdf.exe'), 'document');
});

test('recognizes compound archive extensions', () => {
  assert.equal(resolveFileIconType('backup.tar.gz'), 'archive');
  assert.equal(resolveFileIconType('backup.TAR.BZ2'), 'archive');
  assert.equal(resolveFileIconType('backup.tar.xz'), 'archive');
});

test('recognizes dotfiles that have an explicit mapping', () => {
  assert.equal(resolveFileIconType('.env'), 'code');
});

test('uses the document icon for missing and unknown extensions', () => {
  assert.equal(resolveFileIconType('README'), 'document');
  assert.equal(resolveFileIconType('artifact.unknown'), 'document');
  assert.equal(resolveFileIconType(''), 'document');
});

test('accepts Unix and Windows paths without misreading directory dots', () => {
  assert.equal(resolveFileIconType('/workspace.v2/output/report.pdf'), 'pdf');
  assert.equal(resolveFileIconType('C:\\workspace.v2\\output\\report.docx'), 'word');
  assert.equal(resolveFileIconType('/workspace.v2/output/README'), 'document');
});

test('formats extension labels from basenames', () => {
  assert.equal(getFileExtensionLabel('/workspace.v2/output/REPORT.PDF'), 'pdf');
  assert.equal(getFileExtensionLabel('C:\\workspace.v2\\output\\report.docx'), 'docx');
  assert.equal(getFileExtensionLabel('README'), '');
  assert.equal(getFileExtensionLabel('.env'), '');
});

test('renders from either filename or an explicit icon type', () => {
  const inferredPdf = renderToStaticMarkup(createElement(FileIcon, { fileName: 'report.pdf' }));
  const explicitPdf = renderToStaticMarkup(createElement(FileIcon, { iconType: 'pdf' }));

  assert.equal(inferredPdf, explicitPdf);
});

test('renders the generic document icon when no source is provided', () => {
  const defaultIcon = renderToStaticMarkup(createElement(FileIcon));
  const explicitDocument = renderToStaticMarkup(createElement(FileIcon, { iconType: 'document' }));

  assert.equal(defaultIcon, explicitDocument);
});
