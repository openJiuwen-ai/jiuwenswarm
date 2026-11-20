/**
 * Which document this is: a GUID the document keeps in its own settings.
 *
 * The GUID is the attach ``resource.key``. It travels inside the file, so a
 * save-as, a rename or a move keeps it, while nothing about the file's URL or
 * path goes into it. Which conversations belong to the document is the
 * backend's to know, not the document's.
 */

export type DocumentId = string & { readonly __brand: 'DocumentId' };

export const DOCUMENT_ID_SETTING = 'jiuwenswarm.doc_id';
/** Office opens the add-in's task pane whenever a document with this setting opens. */
const AUTO_SHOW_SETTING = 'Office.AutoShowTaskpaneWithDocument';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The part of ``Office.context.document.settings`` the identity uses. */
export interface DocumentSettings {
  get(name: string): unknown;
  set(name: string, value: unknown): void;
  saveAsync(callback: (result: { status: unknown }) => void): void;
}

/**
 * The document's GUID, written into it on first use.
 *
 * The settings reach the file only when the user saves the document; a new
 * document closed unsaved gets a new GUID the next time it opens.
 */
export async function documentId(settings: DocumentSettings): Promise<DocumentId> {
  const stored = settings.get(DOCUMENT_ID_SETTING);
  if (typeof stored === 'string' && UUID.test(stored)) return stored as DocumentId;
  const created = crypto.randomUUID() as DocumentId;
  settings.set(DOCUMENT_ID_SETTING, created);
  settings.set(AUTO_SHOW_SETTING, true);
  await new Promise<void>(resolve => settings.saveAsync(() => resolve()));
  return created;
}

/** The part of ``Office.context.document`` that knows the file's location. */
export interface DocumentFile {
  url: string | null;
  getFilePropertiesAsync(callback: (result: { status: unknown; value?: { url?: unknown } }) => void): void;
}

/**
 * How the attach names the document: its file name, else ``noun`` and the GUID's end.
 *
 * ``Office.context.document.url`` is often empty for local files on Mac, so
 * the file properties come first.
 */
export async function documentDisplayName(file: DocumentFile, id: DocumentId, noun: string): Promise<string> {
  const fromProperties = await new Promise<unknown>(resolve => {
    file.getFilePropertiesAsync(result => resolve(
      String(result.status).toLowerCase() === 'succeeded' ? result.value?.url : undefined));
  });
  const fileName = fileNameOf(fromProperties) || fileNameOf(file.url);
  return fileName || `${noun} · ${id.slice(-6).toUpperCase()}`;
}

/**
 * The basename of a file URL or of a bare path.
 *
 * PowerPoint on macOS returns paths such as ``/Users/me/Deck.pptx`` where
 * other hosts return URLs, so URL syntax is not required.
 */
export function fileNameOf(location: unknown): string {
  const raw = typeof location === 'string' ? location.trim() : '';
  if (!raw) return '';
  let path: string;
  try {
    path = new URL(raw).pathname;
  } catch {
    path = raw.split(/[?#]/, 1)[0];
  }
  try {
    path = decodeURIComponent(path);
  } catch {
    // Not percent-encoded: keep the host's text.
  }
  return path.replace(/\\/g, '/').split('/').filter(Boolean).at(-1)?.trim() ?? '';
}
