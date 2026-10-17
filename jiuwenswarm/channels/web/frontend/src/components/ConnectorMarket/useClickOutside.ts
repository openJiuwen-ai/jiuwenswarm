import { useEffect } from 'react';

export function useClickOutside(ref: React.RefObject<HTMLElement>, onOutside: () => void) {
  useEffect(() => {
    function handler(event: MouseEvent) {
      if (ref.current && !ref.current.contains(event.target as Node)) {
        onOutside();
      }
    }
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [ref, onOutside]);
}
