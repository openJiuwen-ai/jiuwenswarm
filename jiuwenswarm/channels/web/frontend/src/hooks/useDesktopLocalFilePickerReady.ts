import { useEffect, useState } from 'react';
import {
  DESKTOP_READY_EVENT,
  installDesktopFileDragAccept,
  isDesktopLocalFilePicker,
  isDesktopShell,
} from '../features/workspace/localFilePicker';

/**
 * Desktop drop/paste must not wait solely on `window.pywebview.api`.
 * `desktop_app.py` sets `window.__JIUWEN_DESKTOP__` and dispatches
 * `jiuwen-desktop-ready` as soon as the webview page finishes loading.
 */
export function useDesktopLocalFilePickerReady(): boolean {
  const [ready, setReady] = useState(() => isDesktopShell() || isDesktopLocalFilePicker());

  useEffect(() => {
    const markReady = () => {
      if (isDesktopShell() || isDesktopLocalFilePicker()) {
        installDesktopFileDragAccept();
        setReady(true);
        return true;
      }
      return false;
    };

    markReady();

    const onReady = () => {
      markReady();
    };
    window.addEventListener('pywebviewready', onReady);
    window.addEventListener(DESKTOP_READY_EVENT, onReady);

    const intervalId = window.setInterval(() => {
      if (markReady()) {
        window.clearInterval(intervalId);
      }
    }, 200);
    const timeoutId = window.setTimeout(() => {
      window.clearInterval(intervalId);
    }, 30000);

    return () => {
      window.removeEventListener('pywebviewready', onReady);
      window.removeEventListener(DESKTOP_READY_EVENT, onReady);
      window.clearInterval(intervalId);
      window.clearTimeout(timeoutId);
    };
  }, []);

  return ready;
}
