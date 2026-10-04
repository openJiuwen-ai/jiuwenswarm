import { useCallback, useEffect, useState, type RefObject } from 'react';
import { useTranslation } from 'react-i18next';
import { submitPreviewAiEdit } from '../../features/previewAiEditBridge';
import type { DocSelection } from './docSelection';
import type { PreviewKind } from './filePreviewModel';
import {
  buildDocSelection,
  composeAiEditPrompt,
  floatingBarPosition,
  isPreviewStyleEditable,
  readDomSelectionText,
  supportsPreviewSelection,
} from './previewSelection';

export type SelectionStyleAction = 'bold' | 'italic' | 'code' | 'link';

type SelectionFloatProps = {
  kind: PreviewKind;
  path: string;
  title: string;
  panelRef: RefObject<HTMLElement | null>;
  editing?: boolean;
  onStyleAction?: (action: SelectionStyleAction, selectedText: string) => void;
  rangeHint?: string;
};

type FloatState = {
  sel: DocSelection;
  top: number;
  left: number;
};

export function SelectionFloat({
  kind,
  path,
  title,
  panelRef,
  editing = false,
  onStyleAction,
  rangeHint,
}: SelectionFloatProps) {
  const { t } = useTranslation();
  const [float, setFloat] = useState<FloatState | null>(null);
  const [composing, setComposing] = useState(false);
  const [instruction, setInstruction] = useState('');

  const clear = useCallback(() => {
    setFloat(null);
    setComposing(false);
    setInstruction('');
  }, []);

  const refreshFromDom = useCallback(() => {
    if (!supportsPreviewSelection(kind)) {
      clear();
      return;
    }
    const panel = panelRef.current;
    if (!panel) {
      clear();
      return;
    }
    const domSel = window.getSelection();
    const text = readDomSelectionText(domSel);
    if (!text || !domSel || domSel.rangeCount === 0) {
      if (!composing) clear();
      return;
    }
    const range = domSel.getRangeAt(0);
    const anchor = range.commonAncestorContainer;
    const anchorEl = anchor.nodeType === Node.ELEMENT_NODE ? (anchor as Element) : anchor.parentElement;
    if (!anchorEl || !panel.contains(anchorEl)) {
      if (!composing) clear();
      return;
    }
    const next = buildDocSelection({
      kind,
      path,
      title,
      selectedText: text,
      range: rangeHint,
    });
    if (!next) {
      if (!composing) clear();
      return;
    }
    const pos = floatingBarPosition(range.getBoundingClientRect(), panel.getBoundingClientRect());
    setFloat({ sel: next, top: pos.top, left: pos.left });
  }, [clear, composing, kind, panelRef, path, rangeHint, title]);

  useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    const onPointerUp = () => {
      queueMicrotask(refreshFromDom);
    };
    const onKeyUp = () => {
      queueMicrotask(refreshFromDom);
    };
    panel.addEventListener('mouseup', onPointerUp);
    panel.addEventListener('keyup', onKeyUp);
    document.addEventListener('selectionchange', refreshFromDom);
    return () => {
      panel.removeEventListener('mouseup', onPointerUp);
      panel.removeEventListener('keyup', onKeyUp);
      document.removeEventListener('selectionchange', refreshFromDom);
    };
  }, [panelRef, refreshFromDom]);

  useEffect(() => {
    clear();
  }, [clear, kind, path, title]);

  if (!float) return null;

  const showStyle = Boolean(editing && path.trim() && isPreviewStyleEditable(kind) && onStyleAction);

  const send = () => {
    const prompt = composeAiEditPrompt(float.sel, instruction);
    submitPreviewAiEdit(prompt);
    window.getSelection()?.removeAllRanges();
    clear();
  };

  return (
    <div
      className="fixed z-50 flex -translate-x-1/2 flex-col gap-1 rounded-md border border-border bg-secondary px-2 py-1.5 shadow-sm"
      style={{ top: float.top, left: float.left }}
      data-testid="artifact-selection-float"
      onMouseDown={event => event.preventDefault()}
    >
      <div className="flex items-center gap-1">
        {showStyle ? (
          <>
            <button
              type="button"
              className="rounded px-1.5 py-0.5 text-xs text-text hover:bg-accent/20"
              data-testid="artifact-style-bold"
              onClick={() => onStyleAction?.('bold', float.sel.preview)}
            >
              B
            </button>
            <button
              type="button"
              className="rounded px-1.5 py-0.5 text-xs italic text-text hover:bg-accent/20"
              data-testid="artifact-style-italic"
              onClick={() => onStyleAction?.('italic', float.sel.preview)}
            >
              I
            </button>
            <button
              type="button"
              className="rounded px-1.5 py-0.5 font-mono text-xs text-text hover:bg-accent/20"
              data-testid="artifact-style-code"
              onClick={() => onStyleAction?.('code', float.sel.preview)}
            >
              {'</>'}
            </button>
            <button
              type="button"
              className="rounded px-1.5 py-0.5 text-xs text-text hover:bg-accent/20"
              data-testid="artifact-style-link"
              onClick={() => onStyleAction?.('link', float.sel.preview)}
            >
              Link
            </button>
          </>
        ) : null}
        <button
          type="button"
          className="rounded px-2 py-0.5 text-xs text-text-link hover:bg-accent/20"
          data-testid="artifact-ai-edit-btn"
          onClick={() => setComposing(true)}
        >
          {t('artifacts.aiEdit')}
        </button>
      </div>
      {composing ? (
        <div className="flex min-w-[220px] items-center gap-1">
          <input
            className="min-w-0 flex-1 rounded border border-border bg-background px-2 py-1 text-xs text-text"
            data-testid="artifact-ai-edit-input"
            placeholder={t('artifacts.aiEditPlaceholder')}
            value={instruction}
            onChange={event => setInstruction(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter') {
                event.preventDefault();
                send();
              }
              if (event.key === 'Escape') {
                event.preventDefault();
                setComposing(false);
                setInstruction('');
              }
            }}
            autoFocus
          />
          <button
            type="button"
            className="shrink-0 rounded bg-accent px-2 py-1 text-xs text-text"
            data-testid="artifact-ai-edit-send"
            onClick={send}
          >
            {t('artifacts.aiEditSend')}
          </button>
        </div>
      ) : null}
    </div>
  );
}
