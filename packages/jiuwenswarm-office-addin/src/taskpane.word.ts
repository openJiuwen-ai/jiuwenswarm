/** The task pane in Word. Its tools come with Word's contract; until then it attaches and chats. */
import { ToolContract } from './appContract';
import { joinRuntimes } from './clientToolDispatcher';
import { startPane } from './startPane';

startPane({
  platform: 'microsoft',
  surface: 'document',
  host: 'Word',
  clientApp: 'office-word',
  appId: 'office_word',
  contract: new ToolContract([]),
}, () => ({ runtime: joinRuntimes() }));
