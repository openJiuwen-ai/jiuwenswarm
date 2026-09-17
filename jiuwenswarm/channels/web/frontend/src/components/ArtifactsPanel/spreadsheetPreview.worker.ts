import { parseSpreadsheetWorkbook } from './spreadsheetWorkbookParser';
import { isOoxmlArchiveLimitError } from './ooxmlArchiveLimits';
import type { SpreadsheetWorkerRequest, SpreadsheetWorkerResponse } from './spreadsheetPreviewModel';

const workerScope = self as unknown as {
  onmessage: ((event: MessageEvent<SpreadsheetWorkerRequest>) => void) | null;
  postMessage: (message: SpreadsheetWorkerResponse) => void;
};

workerScope.onmessage = event => {
  if (event.data.type !== 'parse') return;
  void parseSpreadsheetWorkbook(event.data.buffer)
    .then(workbook => {
      workerScope.postMessage({ type: 'ready', workbook });
    })
    .catch(error => {
      workerScope.postMessage({
        type: 'error',
        code: isOoxmlArchiveLimitError(error) ? 'resource-limit' : 'parse-error',
        message: error instanceof Error ? error.message : 'Unable to parse the workbook',
      });
    });
};
