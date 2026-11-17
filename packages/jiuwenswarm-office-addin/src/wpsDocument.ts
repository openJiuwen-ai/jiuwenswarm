/**
 * The WPS presentation a pane is for. WPS has one task pane per window, not per document, so the add-in's shell
 * (``public/wps/wpp/main.js``) writes the document's GUID into its custom document properties under
 * ``DOCUMENT_ID_SETTING``, as Office keeps it in the document's settings, and opens the pane with ``?doc=<GUID>``.
 */
import { copy } from './copy';
import { DOCUMENT_ID_SETTING, type DocumentId } from './documentIdentity';
import type { WpsObject } from './wpsPowerPoint';

/** The WPS application the page runs in: ``Application``, or ``wps`` on builds that name it so. */
export function wpsApplication(): WpsObject {
  const scope = globalThis as { Application?: WpsObject; wps?: WpsObject };
  const application = scope.Application ?? scope.wps;
  if (!application) throw new Error('This page runs only inside WPS Presentation');
  return application;
}

/**
 * The open presentation whose GUID is ``id``. A copy of a file carries the file's GUID; the shell gives a copy open
 * beside its original a GUID of its own before it opens a pane, so two that still share one are refused.
 */
export function presentationWithId(application: WpsObject, id: DocumentId): WpsObject {
  const presentations = application.Presentations;
  const matches: WpsObject[] = [];
  for (let index = 1; index <= presentations.Count; index += 1) {
    const presentation = presentations.Item(index);
    if (documentIdOf(presentation) === id) matches.push(presentation);
  }
  if (matches.length > 1) throw new Error(copy.presentationShared);
  if (!matches.length) throw new Error(copy.presentationClosed);
  return matches[0];
}

function documentIdOf(presentation: WpsObject): string | undefined {
  const properties = presentation.CustomDocumentProperties;
  for (let index = 1; index <= properties.Count; index += 1) {
    const property = properties.Item(index);
    if (property.Name === DOCUMENT_ID_SETTING) return String(property.Value);
  }
  return undefined;
}
