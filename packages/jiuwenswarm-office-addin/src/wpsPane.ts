/** A task pane in one WPS app: the document the shell opened it for, the app's tool runtime and the Gateway. */
import type { OfficeApp, OfficeHost } from './appContract';
import { copy, documentCopy, reasonOf } from './copy';
import type { DocumentId } from './documentIdentity';
import { HostWriteQueue } from './hostWriteQueue';
import { followSystemTheme } from './officeTheme';
import { type HostTools, openPane } from './startPane';
import { Tray } from './view/tray';
import { documentWithId, wpsApplication, type WpsObject } from './wpsDocument';
import { isolateTaskPaneInput } from './wpsInput';

/** What a WPS app's entry builds its tool runtime from. */
export interface WpsHostContext {
  application: WpsObject;
  /** The pane's document: a presentation, workbook or document of the WPS object model. */
  document: WpsObject;
  writes: HostWriteQueue;
  displayName: () => Promise<string>;
}

export function startWpsPane(app: OfficeApp, host: OfficeHost, tools: (context: WpsHostContext) => HostTools): void {
  followSystemTheme();
  start(app, host, tools).catch(error => showFailure(app, error));
}

async function start(app: OfficeApp, host: OfficeHost, tools: (context: WpsHostContext) => HostTools): Promise<void> {
  const application = wpsApplication();
  const id = new URLSearchParams(location.search).get('doc') as DocumentId;
  const document = documentWithId(application, app.surface, id);
  isolateTaskPaneInput(application, window.document);
  const writes = new HostWriteQueue();
  const displayName = async (): Promise<string> => String(document.Name);
  await openPane(app, { documentId: id, displayName, host, writes,
    ...tools({ application, document, writes, displayName }),
    // The shell shows the pane of the document the user works in; a question waits there.
    showPane: () => undefined });
}

/** The pane could not open on its document: say why, with a retry, instead of a blank page. */
function showFailure(app: OfficeApp, error: unknown): void {
  const tray = new Tray();
  tray.update([{ key: 'pane', tone: 'error', icon: 'alert',
    text: copy.paneFailed(documentCopy[app.surface].noun, reasonOf(error)),
    action: { label: copy.retry, run: () => location.reload() } }]);
  document.getElementById('app')!.replaceChildren(tray.element);
}
