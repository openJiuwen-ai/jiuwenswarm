import { useEffect, useState } from 'react';

export function useProcessTreeCollapse(autoCollapse: boolean, resetKey = '') {
  const [collapsed, setCollapsed] = useState(autoCollapse);

  useEffect(() => {
    if (autoCollapse) {
      setCollapsed(true);
    }
  }, [autoCollapse, resetKey]);

  return [collapsed, setCollapsed] as const;
}
