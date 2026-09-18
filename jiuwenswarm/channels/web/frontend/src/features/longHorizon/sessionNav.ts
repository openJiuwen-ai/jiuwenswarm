export const OPEN_SESSION_EVENT = 'jiuwen:open-session';

export type OpenSessionDetail = {
  sessionId: string;
  title?: string;
  kickQuery?: string;
  taskId?: string;
  stageId?: string;
};

export function requestOpenSession(detail: OpenSessionDetail): void {
  window.dispatchEvent(new CustomEvent<OpenSessionDetail>(OPEN_SESSION_EVENT, { detail }));
}
