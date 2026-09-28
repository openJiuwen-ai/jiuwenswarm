import { createContext, useCallback, useContext, useMemo, useState, type ReactNode, type SetStateAction } from 'react';

const TimelineRowStateContext = createContext<{ values: Map<string, unknown>; prefix: string } | null>(null);

export function TimelineRowStateProvider({
  values,
  prefix,
  children,
}: {
  values: Map<string, unknown>;
  prefix: string;
  children: ReactNode;
}) {
  const context = useMemo(() => ({ values, prefix }), [values, prefix]);
  return <TimelineRowStateContext.Provider value={context}>{children}</TimelineRowStateContext.Provider>;
}

/** Keep interaction state when virtualization unmounts an offscreen row. */
export function useTimelineRowState<T>(name: string, initialValue: T | (() => T)) {
  const context = useContext(TimelineRowStateContext);
  const key = `${context?.prefix}/${name}`;
  const [value, setValue] = useState<T>(() => {
    if (context?.values.has(key)) return context.values.get(key) as T;
    return typeof initialValue === 'function' ? (initialValue as () => T)() : initialValue;
  });
  const updateValue = useCallback(
    (action: SetStateAction<T>) => {
      setValue((previous) => {
        const next = typeof action === 'function' ? (action as (value: T) => T)(previous) : action;
        context?.values.set(key, next);
        return next;
      });
    },
    [context, key],
  );
  return [value, updateValue] as const;
}
