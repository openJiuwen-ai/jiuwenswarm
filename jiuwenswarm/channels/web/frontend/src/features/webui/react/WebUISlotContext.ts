import { createContext, useContext } from 'react';
import type { WebUINormalizedTarget } from '../contracts/normalized.ts';

export interface WebUIPageTarget extends WebUINormalizedTarget {
  slotId: 'application.menu.main' | 'task.function.main';
}

export interface WebUISlotContextValue {
  sessionId: string | null;
  onOpenPage?: (target: WebUIPageTarget) => void;
  activePage?: WebUIPageTarget | null;
}

export const WebUISlotContext = createContext<WebUISlotContextValue>({ sessionId: null });

export function useWebUISlotContext(): WebUISlotContextValue {
  return useContext(WebUISlotContext);
}
