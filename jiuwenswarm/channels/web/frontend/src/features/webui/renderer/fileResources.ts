/** Preview data supplied by the plugin; URLs are issued by the host file service. */
export interface WebUIFileContent {
  name: string;
  path: string;
  mimeType?: string;
  size?: number;
  content?: string;
  downloadUrl?: string;
}

export const MAX_FILE_TEXT_PREVIEW_BYTES = 1024 * 1024;

export type WebUIFileReadFailure = 'missing' | 'unsupported' | 'too_large' | 'forbidden' | 'unavailable' | 'error';
