/** The task pane in Excel. Its tools come with Excel's contract; until then it attaches and chats. */
import { ToolContract } from './appContract';
import { joinRuntimes } from './clientToolDispatcher';
import { startPane } from './startPane';

startPane({
  platform: 'microsoft',
  surface: 'workbook',
  host: 'Excel',
  clientApp: 'office-excel',
  appId: 'office_excel',
  contract: new ToolContract([]),
}, () => ({ runtime: joinRuntimes() }));
