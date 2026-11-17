/** The task pane in Word. */
import { joinRuntimes } from './clientToolDispatcher';
import { DocEditReview } from './docEditReview';
import { hostEnums } from './officeCodeRunner';
import { startPane } from './startPane';
import { editCards } from './view/editCards';
import { WORD } from './wordApp';
import { exportPdf, workerPages } from './wordPages';
import type { WordRun } from './wordPassage';
import { WordState } from './wordState';
import { wordRuntime } from './wordTools';

startPane(WORD, ({ office, host, writes }) => {
  const run: WordRun = batch => Word.run(batch);
  const reviews = new DocEditReview(office.document.settings, run, writes);
  const state = new WordState(run, office, host, reviews, writes);
  // The user's edits are heard on a context of the pane's own, which lives as long as the pane.
  void state.watch(new Word.RequestContext()).catch(() => undefined);
  const pages = office.diagnostics.platform === Office.PlatformType.OfficeOnline ? undefined
    : async () => workerPages(await exportPdf(office.document));
  return {
    runtime: joinRuntimes(wordRuntime({ run, host, enums: hostEnums(Word), reviews, pages })),
    clientState: sessionId => state.forMessage(sessionId),
    cards: editCards(reviews),
  };
});
