import { useEffect, useState } from 'react';

export const BUILTIN_PANEL_KEY = 'builtin-tools';

/** Host-owned page visibility. Plugin updates cannot reopen a dismissed instance. */
export function usePanelPages<T extends { key: string }>(pages: T[], scope: string) {
  const [closedKeys, setClosedKeys] = useState<ReadonlySet<string>>(() => new Set());
  const [selection, setSelection] = useState<{ scope: string; key: string } | null>(null);
  const visiblePages = pages.filter((page) => !closedKeys.has(page.key));
  const selectedPageExists = visiblePages.some((page) => page.key === selection?.key);
  const activePage = selection?.scope === scope && selectedPageExists
    ? selection.key : BUILTIN_PANEL_KEY;

  useEffect(() => {
    // Removed pages and session changes must not leave a selection that later reappears.
    if (selection && (selection.scope !== scope || !selectedPageExists)) {
      setSelection(null);
    }
  }, [selection, scope, selectedPageExists]);

  const selectPage = (key: string) => {
    if (key === BUILTIN_PANEL_KEY) setSelection(null);
    else if (visiblePages.some((page) => page.key === key)) setSelection({ scope, key });
  };

  const closePage = (key: string) => {
    const index = visiblePages.findIndex((page) => page.key === key);
    if (index < 0) return;
    setClosedKeys((previous) => new Set([...previous, key]));
    if (activePage === key) {
      const next = visiblePages[index + 1] ?? visiblePages[index - 1];
      setSelection(next ? { scope, key: next.key } : null);
    }
  };

  return { visiblePages, activePage, selectPage, closePage };
}
