import { useCallback, useEffect, useRef, useState } from 'react';
import { webRequest } from '../../services/webClient';
import { SETTINGS_CONFIG_CHANGED_EVENT } from '../settings/services/settingsContract';
import type { MultimodalSnapshot } from '../settings/services/multimodalSettings';

/** Read the shared tool bindings. Design never writes its own generation selection. */
export function useDesignerModelConfiguration() {
  const [snapshot, setSnapshot] = useState<MultimodalSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const sequence = useRef(0);
  const reload = useCallback(async () => {
    const request = ++sequence.current;
    setLoading(true);
    setFailed(false);
    try {
      const result = await webRequest<MultimodalSnapshot>('multimodal.models.list', {}, { timeoutMs: 30_000 });
      if (request === sequence.current) setSnapshot(result);
    } catch {
      if (request === sequence.current) setFailed(true);
    } finally {
      if (request === sequence.current) setLoading(false);
    }
  }, []);
  useEffect(() => {
    const refresh = () => { void reload(); };
    refresh();
    window.addEventListener(SETTINGS_CONFIG_CHANGED_EVENT, refresh);
    window.addEventListener('focus', refresh);
    return () => {
      sequence.current += 1;
      window.removeEventListener(SETTINGS_CONFIG_CHANGED_EVENT, refresh);
      window.removeEventListener('focus', refresh);
    };
  }, [reload]);
  return { snapshot, loading, failed, reload };
}
