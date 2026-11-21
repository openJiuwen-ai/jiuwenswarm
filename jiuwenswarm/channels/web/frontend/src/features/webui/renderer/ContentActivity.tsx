import { createContext, useContext, type ReactNode } from 'react';

const ContentActivity = createContext(true);

export function useContentActivity(): boolean { return useContext(ContentActivity); }

/** Nested inactive content cannot reactivate descendants. */
export function WebUIContentActivity({ active, children }: { active: boolean; children: ReactNode }) {
  const parentActive = useContentActivity();
  return <ContentActivity.Provider value={parentActive && active}>{children}</ContentActivity.Provider>;
}
