import type { WebUISlotRule } from './types.ts';

export const webUISlots: readonly WebUISlotRule[] = [
  { id: 'conversation.input.above', display: 'list', maxVisible: 3 },
  { id: 'conversation.input.below', display: 'list', maxVisible: 2 },
  { id: 'conversation.panel.right', display: 'list', rootComponents: ['Panel', 'Iframe'] },
  // These navigation and settings hosts require a real session_id.
  { id: 'application.menu', display: 'list' },
  { id: 'application.menu.main', display: 'single', rootComponents: ['Iframe'] },
  { id: 'task.function', display: 'list' },
  { id: 'task.function.main', display: 'single', rootComponents: ['Iframe'] },
  { id: 'application.settings', display: 'list' },
  { id: 'application.entry', display: 'single', scope: 'application' },
  { id: 'application.sidebar.footer', display: 'single', scope: 'application' },
  { id: 'conversation.session.content.after', display: 'list' },
  { id: 'conversation.turn.footer.actions', display: 'list' },
  { id: 'conversation.header.title.after', display: 'list' },
  { id: 'conversation.header.actions', display: 'list' },
  { id: 'message.assistant.meta', display: 'list' },
];
