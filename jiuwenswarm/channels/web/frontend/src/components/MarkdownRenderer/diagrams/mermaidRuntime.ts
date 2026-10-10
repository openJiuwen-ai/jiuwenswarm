import type { MermaidConfig } from 'mermaid';

export interface MermaidRuntime {
  initialize(config: MermaidConfig): void;
  render(id: string, code: string): Promise<{ svg: string }>;
}

export type MermaidRuntimeLoader = () => Promise<MermaidRuntime>;
export type MermaidSvgRenderer = (id: string, code: string) => Promise<string>;

export const MERMAID_CONFIG: MermaidConfig = {
  startOnLoad: false,
  suppressErrorRendering: true,
  securityLevel: 'strict',
  htmlLabels: false,
  flowchart: { useMaxWidth: false },
  theme: 'default',
};

export function createMermaidRenderer(loadRuntime: MermaidRuntimeLoader): MermaidSvgRenderer {
  let runtimePromise: Promise<MermaidRuntime> | null = null;

  function getRuntime(): Promise<MermaidRuntime> {
    if (!runtimePromise) {
      runtimePromise = loadRuntime()
        .then(runtime => {
          runtime.initialize(MERMAID_CONFIG);
          return runtime;
        })
        .catch(error => {
          runtimePromise = null;
          throw error;
        });
    }
    return runtimePromise;
  }

  return async function renderMermaidSvg(id: string, code: string): Promise<string> {
    const runtime = await getRuntime();
    const result = await runtime.render(id, code);
    return result.svg;
  };
}

export const renderMermaidSvg = createMermaidRenderer(async () => (await import('mermaid')).default);
