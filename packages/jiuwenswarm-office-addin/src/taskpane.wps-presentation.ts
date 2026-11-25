/** The task pane in WPS Presentation: the PowerPoint tools, run over WPS's object model. */
import { joinRuntimes } from './clientToolDispatcher';
import { WPS_PRESENTATION } from './powerPointApp';
import { powerPointPackageRuntime, workerSandbox } from './powerPointPackageTools';
import { powerPointReadRuntime, selectedSlides } from './powerPointReadTools';
import { powerPointSlideDeck, powerPointWriteRuntime } from './powerPointWriteTools';
import { selectionLine } from './selectionLine';
import { SlideSnapshots } from './slideSnapshots';
import { rasterizeSvg } from './svgRaster';
import { startWpsPane } from './wpsPane';
import { WPS_HOST, wpsPowerPointRun } from './wpsPowerPoint';

startWpsPane(WPS_PRESENTATION, WPS_HOST, ({ application, document: presentation, writes, displayName }) => {
  const run = wpsPowerPointRun(presentation, application);
  // WPS keeps its own undo, so the pane offers no undo of a turn and keeps no turn: a call's journal only puts a
  // failed write's slides back.
  const journals = new SlideSnapshots(powerPointSlideDeck(run), writes, { turns: 0, bytes: 0 });
  const languages = { displayLanguage: navigator.language, contentLanguage: navigator.language };
  return {
    runtime: joinRuntimes(
      powerPointReadRuntime({ run, host: WPS_HOST, documentName: displayName, languages }),
      powerPointWriteRuntime({ run, host: WPS_HOST, journal: callId => journals.journal(callId) }),
      powerPointPackageRuntime({ run, host: WPS_HOST, journal: callId => journals.journal(callId),
        sandbox: workerSandbox(() => new Worker(new URL('./ooxmlSandbox.worker.ts', import.meta.url),
          { type: 'module' })),
        rasterize: rasterizeSvg, forgetUndo: () => journals.forget(), insertTakesDeckDesign: true })),
    // WPS raises this for a slide or shape selected, not for text selected inside a shape.
    selection: selectionLine(moved => application.ApiEvent?.AddApiEventListener?.('WindowSelectionChange', moved),
      () => selectedSlides(run)),
  };
});
