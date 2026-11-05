import type { WebUISlotRule } from './types.ts';

export const webUISlots: readonly WebUISlotRule[] = [
  { id: 'conversation.input.above', display: 'list', maxVisible: 3 },
  { id: 'conversation.input.below', display: 'list', maxVisible: 2 },
  { id: 'conversation.panel.right', display: 'list', rootComponents: ['Panel', 'Iframe'], strictRoot: true },
  // Menu hosts require a real session_id; task navigation belongs to the application.
  { id: 'application.menu', display: 'list' },
  { id: 'application.menu.main', display: 'single', rootComponents: ['Iframe'] },
  { id: 'task.function', display: 'list', scope: 'application' },
  { id: 'task.function.main', display: 'single', scope: 'application', rootComponents: ['Iframe'] },
  { id: 'application.settings', display: 'list', additionalScopes: ['application'] },
  { id: 'application.entry', display: 'single', scope: 'application' },
  { id: 'application.sidebar.footer', display: 'single', scope: 'application' },
  // Gateway-only full-window layer below the workspace.
  { id: 'application.background', display: 'single', scope: 'application', rootComponents: ['MediaSurface'], strictRoot: true },
  // Gateway-only startup layer above the workspace, played by the splash gate.
  { id: 'application.splash', display: 'single', scope: 'application', rootComponents: ['MediaSurface'], strictRoot: true },
  // Gateway-only settings pages, one per package, shown in the Extensions settings module (S_10).
  { id: 'application.settings.page', display: 'list', scope: 'application' },
  { id: 'conversation.session.content.after', display: 'list' },
  { id: 'conversation.turn.footer.actions', display: 'list' },
  { id: 'conversation.header.title.after', display: 'list' },
  { id: 'conversation.header.actions', display: 'list' },
  { id: 'message.assistant.meta', display: 'list' },
];
