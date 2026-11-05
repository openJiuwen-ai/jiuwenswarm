import { createContext, useContext } from 'react';
import type { WebUIUploadService } from '../../../transfers/uploads.ts';

/** Host-provided import service; without one the control stays disabled. */
export const WebUIUploadContext = createContext<WebUIUploadService | null>(null);

export function useWebUIUploadService(): WebUIUploadService | null {
  return useContext(WebUIUploadContext);
}
