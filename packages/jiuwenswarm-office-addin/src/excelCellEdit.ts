/**
 * Excel takes no request while the user types in a cell (``InvalidOperationInCellEditMode``). Before each tool call
 * the pane asks Excel for one property; when the user is editing a cell, the pane asks them to press Enter or Esc,
 * and the call waits on a request run with ``delayForCellEdit``, which Excel answers once the cell is left. Past the
 * wait, or on an Excel that does not hold the request, the call fails and the model asks the user, as the industry
 * add-in does.
 */
import type { ClientToolResponse, ClientToolRuntime } from './clientToolDispatcher';
import { copy } from './copy';
import type { ExcelRun } from './excelTools';
import type { HostLine } from './hostLine';

/** How long a call waits for the cell to be left: Excel on the web is given less, as the industry add-in gives it. */
export const CELL_EDIT_WAIT_MS = { desktop: 30_000, web: 8_000 };

const editing = (error: unknown): boolean => (
  (error as { code?: unknown } | null)?.code === 'InvalidOperationInCellEditMode');

/** A request that reaches Excel: a sync with nothing queued never leaves the pane. */
async function probe(context: Excel.RequestContext): Promise<void> {
  context.workbook.load('name');
  await context.sync();
}

const STILL_EDITING: ClientToolResponse = { ok: false, error: {
  code: 'EXCEL_CELL_EDIT_MODE', retryable: false, category: 'conflict',
  message: 'CellEditTimeoutError: The user is editing a cell, and Excel takes no request until they leave it. Ask the '
    + 'user to press Enter or Esc in the spreadsheet, then try again.',
  details: { outcome: 'not_applied', native_mutation_started: false },
} };

export function waitingForCellEdit<T extends string>(runtime: ClientToolRuntime<T>, { run, delayed, waitMs, line }: {
  run: ExcelRun;
  /** ``Excel.run`` with ``delayForCellEdit``. */
  delayed: ExcelRun;
  waitMs: number;
  /** The tray line that asks the user to leave the cell while any call waits. */
  line: HostLine;
}): ClientToolRuntime<T> {
  let waiting = 0;
  /** Whether Excel takes requests: at once, or once the user left the cell within the wait. */
  const cellLeft = async (): Promise<boolean> => {
    try {
      await run(probe);
      return true;
    } catch (error) {
      // Any other error is the call's to meet.
      if (!editing(error)) return true;
    }
    waiting += 1;
    line.set(copy.cellEditWait);
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      return await Promise.race([delayed(probe).then(() => true, error => !editing(error)),
        new Promise<boolean>(resolve => { timer = setTimeout(() => resolve(false), waitMs); })]);
    } finally {
      clearTimeout(timer);
      waiting -= 1;
      if (!waiting) line.set(undefined);
    }
  };
  return { ...runtime, execute: async call => (await cellLeft() ? runtime.execute(call) : STILL_EDITING) };
}
