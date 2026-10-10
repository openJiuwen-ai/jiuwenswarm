import { useCallback, useEffect, type SetStateAction } from 'react';
import { useTimelineRowState } from './timelineRowState';

export function useProcessTreeCollapse(autoCollapse: boolean, resetKey = '', stateKey = 'process-tree') {
  const [state, setState] = useTimelineRowState(stateKey, { collapsed: autoCollapse, autoCollapse, resetKey });
  useEffect(() => {
    setState((current) =>
      current.autoCollapse === autoCollapse && current.resetKey === resetKey
        ? current
        : { collapsed: autoCollapse || current.collapsed, autoCollapse, resetKey },
    );
  }, [autoCollapse, resetKey, setState]);
  const setCollapsed = useCallback(
    (action: SetStateAction<boolean>) => {
      setState((current) => ({
        ...current,
        collapsed: typeof action === 'function' ? action(current.collapsed) : action,
      }));
    },
    [setState],
  );
  return [state.collapsed, setCollapsed] as const;
}
