/**
 * The WPS document a pane is for. WPS has one task pane per window, not per document, so the add-in's shell
 * (``public/wps/shell.js``) writes the document's GUID into its custom document properties under
 * ``DOCUMENT_ID_SETTING``, as Office keeps it in the document's settings, and opens the pane with ``?doc=<GUID>``.
 */
import type { Surface } from './appContract';
import { copy, documentCopy } from './copy';
import { DOCUMENT_ID_SETTING, type DocumentId } from './documentIdentity';

/** A WPS object: a COM-like graph whose declarations differ by build, typed only at this boundary. */
export type WpsObject = Record<string, any>;

/** The collection of open documents of each WPS app, by the surface it edits. */
const OPEN_DOCUMENTS: Record<Surface, string> = {
  presentation: 'Presentations', workbook: 'Workbooks', document: 'Documents',
};

/** The WPS application the page runs in: ``Application``, or ``wps`` on builds that name it so. */
export function wpsApplication(): WpsObject {
  const scope = globalThis as { Application?: WpsObject; wps?: WpsObject };
  const application = scope.Application ?? scope.wps;
  if (!application) throw new Error('This page runs only inside WPS');
  return application;
}

/**
 * The open document of ``surface`` whose GUID is ``id``. A copy of a file carries the file's GUID; the shell gives
 * a copy open beside its original a GUID of its own before it opens a pane, so two that still share one are refused.
 */
export function documentWithId(application: WpsObject, surface: Surface, id: DocumentId): WpsObject {
  const documents = application[OPEN_DOCUMENTS[surface]];
  const matches: WpsObject[] = [];
  for (let index = 1; index <= documents.Count; index += 1) {
    const document = documents.Item(index);
    if (documentIdOf(document) === id) matches.push(document);
  }
  const { noun } = documentCopy[surface];
  if (matches.length > 1) throw new Error(copy.documentShared(noun));
  if (!matches.length) throw new Error(copy.documentClosed(noun));
  return matches[0];
}

function documentIdOf(document: WpsObject): string | undefined {
  const properties = document.CustomDocumentProperties;
  for (let index = 1; index <= properties.Count; index += 1) {
    const property = properties.Item(index);
    if (property.Name === DOCUMENT_ID_SETTING) return String(property.Value);
  }
  return undefined;
}
