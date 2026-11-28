import type { WebUISlotRule } from './types.ts';

export const webUISlots: readonly WebUISlotRule[] = [
  { id: 'conversation.input.above', display: 'list', maxVisible: 3 },
  { id: 'conversation.input.below', display: 'list', maxVisible: 2 },
  { id: 'conversation.panel.right', display: 'list', rootComponents: ['Panel', 'Iframe'], strictRoot: true, rerender: 'keep-mounted' },
  // Menu and task navigation hosts belong to the application so switching sessions keeps them mounted.
  { id: 'application.menu', display: 'list', scope: 'application' },
  { id: 'application.menu.main', display: 'single', scope: 'application', rootComponents: ['Iframe'], rerender: 'keep-mounted' },
  { id: 'task.function', display: 'list', scope: 'application' },
  { id: 'task.function.main', display: 'single', scope: 'application', rootComponents: ['Iframe'], rerender: 'keep-mounted' },
  { id: 'application.settings', display: 'list', additionalScopes: ['application'] },
  { id: 'application.entry', display: 'single', scope: 'application' },
  // Keep maxVisible in sync with SlotSpec.max_instances in extensions_v2/webui/constants.py.
  { id: 'application.sidebar.footer', display: 'list', scope: 'application', maxVisible: 3 },
  { id: 'application.presentation', display: 'list', scope: 'application',
    rootComponents: ['ApplicationSurface'], strictRoot: true, rerender: 'keep-mounted' },
  // Gateway-only full-window layer below the workspace.
  // A background keeps running between frames: dim or blur updates must not reload its page.
  { id: 'application.background', display: 'single', scope: 'application', rootComponents: ['MediaSurface'], strictRoot: true,
    rerender: 'keep-mounted' },
  // Gateway-only startup layer above the workspace, played by the splash gate.
  { id: 'application.splash', display: 'single', scope: 'application', rootComponents: ['MediaSurface'], strictRoot: true },
  // Gateway-only declaration of appearance providers: nothing renders here, the gate sends its ready (S_09).
  { id: 'application.appearance', display: 'list', scope: 'application' },
  { id: 'conversation.session.content.after', display: 'list' },
  { id: 'conversation.turn.footer.actions', display: 'list' },
  // Keep maxVisible in sync with SlotSpec.max_instances in extensions_v2/webui/constants.py.
  { id: 'conversation.header.title.after', display: 'list', maxVisible: 2 },
  { id: 'conversation.header.actions', display: 'list', maxVisible: 3 },
  { id: 'message.assistant.meta', display: 'list' },
];
