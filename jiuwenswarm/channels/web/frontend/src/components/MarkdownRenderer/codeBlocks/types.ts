import type { ComponentType } from 'react';

export interface FencedCodeBlock {
  language: string;
  code: string;
  complete: boolean;
}

export interface FencedCodeRendererProps {
  code: string;
  complete: boolean;
  isStreaming: boolean;
}

export interface FencedCodeAdapter {
  language: string;
  renderWhileStreaming: boolean;
  Renderer: ComponentType<FencedCodeRendererProps>;
}
