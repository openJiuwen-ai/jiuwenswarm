import { createContext } from 'react';

/** Static exports retain artifact cards without mounting their media players. */
export const FileDownloadMediaPreviewContext = createContext(true);
