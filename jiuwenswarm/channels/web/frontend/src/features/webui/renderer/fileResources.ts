/** Preview data supplied by the plugin's tree; the renderer never reads files. */
export interface WebUIFileContent {
  name: string;
  path: string;
  mimeType?: string;
  size?: number;
  content: string;
}

export type WebUIFileReadFailure = 'missing' | 'unsupported' | 'too_large' | 'forbidden' | 'unavailable' | 'error';
