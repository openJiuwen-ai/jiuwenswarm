/** The task pane in Excel. */
import { joinRuntimes } from './clientToolDispatcher';
import { EXCEL } from './excelApp';
import { CELL_EDIT_WAIT_MS, waitingForCellEdit } from './excelCellEdit';
import { ExcelState, selectedCells } from './excelState';
import { excelRuntime, type ExcelRun } from './excelTools';
import { HostLine } from './hostLine';
import { hostEnums } from './officeCodeRunner';
import { officeSelectionMoves, selectionLine } from './selectionLine';
import { startPane } from './startPane';

startPane(EXCEL, ({ office, host, writes }) => {
  const run: ExcelRun = batch => Excel.run(batch);
  const state = new ExcelState(run, office, writes);
  // The user's changes are heard on a context of the pane's own, which lives as long as the pane.
  void state.watch(new Excel.RequestContext(), host).catch(() => undefined);
  const waiting = new HostLine();
  const runtime = joinRuntimes(waitingForCellEdit(excelRuntime({ run, host, enums: hostEnums(Excel),
    surface: () => state.current(), writes }), {
    run, delayed: batch => Excel.run({ delayForCellEdit: true }, batch), line: waiting,
    waitMs: office.diagnostics.platform === Office.PlatformType.OfficeOnline ? CELL_EDIT_WAIT_MS.web
      : CELL_EDIT_WAIT_MS.desktop,
  }));
  return { runtime, clientState: sessionId => state.forMessage(sessionId), waiting,
    selection: selectionLine(officeSelectionMoves(office.document), () => selectedCells(run)) };
});
