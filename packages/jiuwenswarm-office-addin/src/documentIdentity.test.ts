import { describe, expect, expectTypeOf, it } from 'vitest';

import {
  DOCUMENT_ID_SETTING, documentDisplayName, documentId, fileNameOf,
  type DocumentFile, type DocumentId, type DocumentSettings,
} from './documentIdentity';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/** Office document settings as the file holds them: what reached ``saveAsync`` is what a save writes. */
function settingsOf(initial: Record<string, unknown> = {}) {
  const values = new Map(Object.entries(initial));
  const saved = new Map<string, unknown>();
  const settings: DocumentSettings = {
    get: name => values.get(name),
    set: (name, value) => { values.set(name, value); },
    saveAsync: callback => {
      values.forEach((value, name) => saved.set(name, value));
      callback({ status: 'succeeded' });
    },
  };
  return { settings, saved };
}

function fileAt(url: string, propertiesUrl = url): DocumentFile {
  return { url, getFilePropertiesAsync: callback => callback({ status: 'succeeded', value: { url: propertiesUrl } }) };
}

describe('document identity', () => {
  it('writes a new GUID into a document that has none, which then opens with the task pane', async () => {
    const { settings, saved } = settingsOf();
    const id = await documentId(settings);
    expect(id).toMatch(UUID);
    expect(saved.get(DOCUMENT_ID_SETTING)).toBe(id);
    expect(saved.get('Office.AutoShowTaskpaneWithDocument')).toBe(true);
    expect(await documentId(settings)).toBe(id);
  });

  it('keeps the GUID the document carries, wherever the file is saved', async () => {
    const stored = '3f1c2a9e-7b4d-4e8a-9c21-5d6f7a8b9c0d';
    const { settings, saved } = settingsOf({ [DOCUMENT_ID_SETTING]: stored });
    const original = fileAt('/Users/me/Deck.pptx');
    const savedAs = fileAt('https://contoso.sharepoint.com/sites/team/Shared%20Documents/Deck%20final.pptx');

    expect(await documentId(settings)).toBe(stored);
    expect(saved.size).toBe(0);
    expect(await documentDisplayName(original, stored as DocumentId)).toBe('Deck.pptx');
    expect(await documentId(settings)).toBe(stored);
    expect(await documentDisplayName(savedAs, stored as DocumentId)).toBe('Deck final.pptx');
  });

  it('replaces a stored value that is not a GUID', async () => {
    const { settings } = settingsOf({ [DOCUMENT_ID_SETTING]: 'office-deck' });
    const id = await documentId(settings);
    expect(id).toMatch(UUID);
    expect(settings.get(DOCUMENT_ID_SETTING)).toBe(id);
  });

  it('names the document after its file, else after the end of its GUID', async () => {
    const id = '3f1c2a9e-7b4d-4e8a-9c21-5d6f7a8b9c0d' as DocumentId;
    expect(await documentDisplayName(fileAt('', '/Users/me/My Deck.pptx'), id)).toBe('My Deck.pptx');
    expect(await documentDisplayName(fileAt('C:\\Users\\me\\Plan.pptx', ''), id)).toBe('Plan.pptx');
    const unsaved: DocumentFile = { url: null, getFilePropertiesAsync: callback => callback({ status: 'failed' }) };
    expect(await documentDisplayName(unsaved, id)).toBe('演示文稿 · 8B9C0D');
    expect(fileNameOf('https://example.test/docs/Quarterly.pptm?download=1')).toBe('Quarterly.pptm');
    expect(fileNameOf('file:///Users/test/%E5%AD%A3%E5%BA%A6.pptx')).toBe('季度.pptx');
  });

  it('reads the Office objects as they are', () => {
    expectTypeOf<Office.Settings>().toMatchTypeOf<DocumentSettings>();
    expectTypeOf<Office.Document>().toMatchTypeOf<DocumentFile>();
  });
});
