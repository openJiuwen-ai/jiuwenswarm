/** The task pane in Excel. */
import { joinRuntimes } from './clientToolDispatcher';
import { EXCEL } from './excelApp';
import { ExcelState } from './excelState';
import { excelRuntime, type ExcelRun } from './excelTools';
import { hostEnums } from './officeCodeRunner';
import { startPane } from './startPane';

startPane(EXCEL, ({ office, host, writes }) => {
  const run: ExcelRun = batch => Excel.run(batch);
  const state = new ExcelState(run, office, writes);
  // The user's changes are heard on a context of the pane's own, which lives as long as the pane.
  void state.watch(new Excel.RequestContext(), host).catch(() => undefined);
  const runtime = joinRuntimes(excelRuntime({ run, host, enums: hostEnums(Excel), surface: () => state.current(),
    writes }));
  return { runtime, clientState: sessionId => state.forMessage(sessionId) };
});
