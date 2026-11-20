/** The task pane in WPS Spreadsheets: the Excel tools over WPS's object model, and code in it (``execute_wps_js``). */
import { joinRuntimes } from './clientToolDispatcher';
import { WPS_SPREADSHEET } from './excelApp';
import { screenWpsSheetWrite, WPS_SHEET_UNREACHABLE } from './excelGuard';
import { ExcelState } from './excelState';
import { excelRuntime } from './excelTools';
import { wpsCodeRuntime } from './officeCodeRunner';
import { WPS_EXCEL_HOST, wpsExcelRun } from './wpsExcel';
import { startWpsPane } from './wpsPane';

startWpsPane(WPS_SPREADSHEET, WPS_EXCEL_HOST, ({ application, document: workbook, writes }) => {
  const run = wpsExcelRun(workbook);
  const state = new ExcelState(run, { contentLanguage: navigator.language, displayLanguage: navigator.language },
    writes);
  return {
    runtime: joinRuntimes(
      excelRuntime({ run, host: WPS_EXCEL_HOST, surface: () => state.current(), writes }),
      wpsCodeRuntime({ name: WPS_SPREADSHEET.host, parameter: 'workbook', document: workbook,
        constants: application.Enum ?? {}, screen: screenWpsSheetWrite, unreachable: WPS_SHEET_UNREACHABLE })),
    clientState: sessionId => state.forMessage(sessionId),
  };
});
