/** The task pane in WPS Writer: the Word tools over WPS's object model, and code in it (``execute_wps_js``). */
import { joinRuntimes } from './clientToolDispatcher';
import { DocEditReview } from './docEditReview';
import { wpsCodeRuntime } from './officeCodeRunner';
import { editCards } from './view/editCards';
import { WPS_WRITER } from './wordApp';
import { screenWpsWriterWrite, WPS_WRITER_UNREACHABLE } from './wordGuard';
import { workerPages } from './wordPages';
import { WordState } from './wordState';
import { wordRuntime } from './wordTools';
import { startWpsPane } from './wpsPane';
import { WPS_WRITER_HOST, wpsDocumentPdf, wpsDocumentSettings, wpsWordRun } from './wpsWriter';

startWpsPane(WPS_WRITER, WPS_WRITER_HOST, ({ application, document, writes }) => {
  const run = wpsWordRun(document);
  const reviews = new DocEditReview(wpsDocumentSettings(document), run, writes);
  // WPS names no authoring language; its display language stands in for both.
  const state = new WordState(run, { contentLanguage: '', displayLanguage: navigator.language }, WPS_WRITER_HOST,
    reviews, writes, WPS_WRITER.platform);
  return {
    runtime: joinRuntimes(
      wordRuntime({ run, host: WPS_WRITER_HOST, reviews,
        pages: async () => workerPages(await wpsDocumentPdf(document, application)) }),
      wpsCodeRuntime({ name: WPS_WRITER.host, parameter: 'document', document, constants: application.Enum ?? {},
        screen: screenWpsWriterWrite, unreachable: WPS_WRITER_UNREACHABLE })),
    clientState: sessionId => state.forMessage(sessionId),
    cards: editCards(reviews),
  };
});
