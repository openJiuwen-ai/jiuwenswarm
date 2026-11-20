import { useMemo, type ReactNode } from 'react';
import { WebUISlotContext, type WebUISlotContextValue } from './WebUISlotContext.ts';

/** The application injects view context and navigation; slots never read business stores. */
export function WebUISlotProvider({ sessionId, onOpenPage, activePage, children }: WebUISlotContextValue & { children: ReactNode }) {
  const value = useMemo(() => ({ sessionId, onOpenPage, activePage }), [sessionId, onOpenPage, activePage]);
  return <WebUISlotContext.Provider value={value}>{children}</WebUISlotContext.Provider>;
}
