import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import {
  ComfyuiImportError,
  buildComfyuiCanvasGraph,
  parseComfyuiWorkflow,
  type ComfyuiWarning,
} from './comfyuiWorkflow';
import { DESIGNER_SUCCESSOR_GAP_Y } from './designerCanvasNodes';
import { useDesignerRunStore } from './designerRunStore';
import { useDesignerStore } from './designerStore';

const WARNING_TOAST_MS = 10_000;

/** Reads ComfyUI workflow files and adds their vLLM-Omni nodes below one another from `origin`. */
export function useComfyuiImport(): (files: File[], origin: { x: number; y: number }) => Promise<void> {
  const { t } = useTranslation();
  const addSubgraph = useDesignerStore((state) => state.addSubgraph);

  const describeWarning = useCallback(
    (warning: ComfyuiWarning) =>
      t(`designer.comfyui.warningItem.${warning.code}`, {
        node: warning.node_id,
        detail: warning.detail,
      }),
    [t],
  );

  return useCallback(
    async (files: File[], origin: { x: number; y: number }) => {
      const errors: string[] = [];
      const warnings: string[] = [];
      let cursorY = origin.y;
      for (const file of files) {
        try {
          const workflow = parseComfyuiWorkflow(await file.text());
          const existing = useDesignerStore.getState().domainGraph?.nodes ?? [];
          const { nodes, edges } = buildComfyuiCanvasGraph({
            workflow,
            existing,
            origin: { x: origin.x, y: cursorY },
          });
          addSubgraph(nodes, edges);
          const bottom = Math.max(...nodes.map((node) => (node.layout?.y ?? cursorY) + (node.layout?.height ?? 0)));
          cursorY = bottom + DESIGNER_SUCCESSOR_GAP_Y * 2;
          if (workflow.warnings.length) {
            warnings.push(
              t('designer.comfyui.warning', {
                file: file.name,
                items: workflow.warnings.map(describeWarning).join('; '),
              }),
            );
          }
        } catch (error) {
          const code = error instanceof ComfyuiImportError ? error.code : 'read_failed';
          errors.push(
            t(`designer.comfyui.error.${code}`, {
              file: file.name,
              detail: error instanceof Error ? error.message : String(error),
            }),
          );
        }
      }
      if (errors.length) {
        useDesignerRunStore.setState({ runError: errors.join('\n') });
      }
      if (warnings.length) {
        const message = warnings.join('\n');
        useDesignerRunStore.setState({ runWarning: message });
        window.setTimeout(() => {
          if (useDesignerRunStore.getState().runWarning === message) {
            useDesignerRunStore.setState({ runWarning: null });
          }
        }, WARNING_TOAST_MS);
      }
    },
    [addSubgraph, describeWarning, t],
  );
}
