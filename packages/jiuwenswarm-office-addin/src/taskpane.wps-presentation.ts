/** The task pane in WPS Presentation: the PowerPoint tools, run over WPS's object model. */
import { joinRuntimes } from './clientToolDispatcher';
import { copy, reasonOf } from './copy';
import type { DocumentId } from './documentIdentity';
import { HostWriteQueue } from './hostWriteQueue';
import { followSystemTheme } from './officeTheme';
import { WPS_PRESENTATION } from './powerPointApp';
import { powerPointPackageRuntime, workerSandbox } from './powerPointPackageTools';
import { powerPointReadRuntime } from './powerPointReadTools';
import { powerPointSlideDeck, powerPointWriteRuntime } from './powerPointWriteTools';
import { SlideSnapshots } from './slideSnapshots';
import { openPane } from './startPane';
import { Tray } from './view/tray';
import { presentationWithId, wpsApplication } from './wpsDocument';
import { isolateTaskPaneInput } from './wpsInput';
import { WPS_HOST, wpsPowerPointRun } from './wpsPowerPoint';

async function start(): Promise<void> {
  const application = wpsApplication();
  const id = new URLSearchParams(location.search).get('doc') as DocumentId;
  const presentation = presentationWithId(application, id);
  isolateTaskPaneInput(application, document);
  const run = wpsPowerPointRun(presentation, application);
  const writes = new HostWriteQueue();
  // WPS keeps its own undo, so the pane offers no undo of a turn and keeps no turn: a call's journal only puts a
  // failed write's slides back.
  const journals = new SlideSnapshots(powerPointSlideDeck(run), writes, { turns: 0, bytes: 0 });
  const languages = { displayLanguage: navigator.language, contentLanguage: navigator.language };
  const displayName = async (): Promise<string> => String(presentation.Name);
  await openPane(WPS_PRESENTATION, {
    documentId: id, displayName, host: WPS_HOST, writes,
    runtime: joinRuntimes(
      powerPointReadRuntime({ run, host: WPS_HOST, documentName: displayName, languages }),
      powerPointWriteRuntime({ run, host: WPS_HOST, journal: callId => journals.journal(callId) }),
      powerPointPackageRuntime({ run, host: WPS_HOST, journal: callId => journals.journal(callId),
        sandbox: workerSandbox(() => new Worker(new URL('./ooxmlSandbox.worker.ts', import.meta.url),
          { type: 'module' })),
        forgetUndo: () => journals.forget(), insertTakesDeckDesign: true })),
    // The shell shows the pane of the document the user works in; a question waits there.
    showPane: () => undefined,
  });
}

/** The pane could not open on its presentation: say why, with a retry, instead of a blank page. */
function showFailure(error: unknown): void {
  const tray = new Tray();
  tray.update([{ key: 'pane', tone: 'error', icon: 'alert', text: copy.paneFailed(reasonOf(error)),
    action: { label: copy.retry, run: () => location.reload() } }]);
  document.getElementById('app')!.replaceChildren(tray.element);
}

followSystemTheme();
start().catch(showFailure);
