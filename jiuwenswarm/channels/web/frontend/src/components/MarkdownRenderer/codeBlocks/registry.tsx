import type { JSX } from 'react';
import { MermaidDiagram } from '../diagrams/MermaidDiagram';
import { SvgDiagram } from '../diagrams/SvgDiagram';
import type { FencedCodeAdapter, FencedCodeBlock, FencedCodeRendererProps } from './types';

function MermaidCodeBlock({ code }: FencedCodeRendererProps): JSX.Element {
  return <MermaidDiagram code={code} />;
}

const FENCED_CODE_ADAPTERS: readonly FencedCodeAdapter[] = [
  {
    language: 'mermaid',
    renderWhileStreaming: false,
    Renderer: MermaidCodeBlock,
  },
  {
    language: 'svg',
    renderWhileStreaming: true,
    Renderer: SvgDiagram,
  },
];

const ADAPTERS_BY_LANGUAGE = new Map(FENCED_CODE_ADAPTERS.map(adapter => [adapter.language, adapter]));

export function getFencedCodeAdapter(block: FencedCodeBlock): FencedCodeAdapter | null {
  const adapter = ADAPTERS_BY_LANGUAGE.get(block.language);
  if (!adapter) return null;
  if (!block.complete && !adapter.renderWhileStreaming) return null;
  return adapter;
}
