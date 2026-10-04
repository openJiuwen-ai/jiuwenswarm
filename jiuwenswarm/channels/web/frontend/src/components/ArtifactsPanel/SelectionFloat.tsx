import { useCallback, useEffect, useState, type ReactNode, type RefObject } from 'react';
import { useTranslation } from 'react-i18next';
import { Link2, Sparkles } from 'lucide-react';
import { submitPreviewAiEdit } from '../../features/previewAiEditBridge';
import type { DocSelection } from './docSelection';
import type { PreviewKind } from './filePreviewModel';
import {
  buildDocSelection,
  floatingBarPosition,
  isPreviewStyleEditable,
  readDomSelectionText,
  supportsPreviewSelection,
} from './previewSelection';
import type { TextWrapStyle } from './previewTextEdit';

export type SelectionStyleAction = TextWrapStyle;

type SelectionFloatProps = {
  kind: PreviewKind;
  path: string;
  title: string;
  panelRef: RefObject<HTMLElement | null>;
  /** When set, also listen for selections inside this iframe document. */
  iframeRef?: RefObject<HTMLIFrameElement | null>;
  onStyleAction?: (action: SelectionStyleAction, selectedText: string) => void;
  rangeHint?: string;
};

type FloatState = {
  sel: DocSelection;
  top: number;
  left: number;
};

function selectionInside(panel: HTMLElement, node: Node | null): boolean {
  if (!node) return false;
  const el = node.nodeType === Node.ELEMENT_NODE ? (node as Element) : node.parentElement;
  return Boolean(el && panel.contains(el));
}

export function SelectionFloat({
  kind,
  path,
  title,
  panelRef,
  iframeRef,
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

  const applySelection = useCallback(
    (domSel: Selection | null | undefined, clientRect: DOMRect | null, root: HTMLElement) => {
      if (!supportsPreviewSelection(kind)) {
        clear();
        return;
      }
      const text = readDomSelectionText(domSel);
      if (!text || !domSel || domSel.rangeCount === 0 || !clientRect) {
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
      const pos = floatingBarPosition(clientRect, root.getBoundingClientRect(), { barHalfWidth: 160 });
      setFloat({ sel: next, top: pos.top, left: pos.left });
    },
    [clear, composing, kind, path, rangeHint, title],
  );

  const refreshFromDom = useCallback(() => {
    const panel = panelRef.current;
    if (!panel) {
      clear();
      return;
    }
    const iframe = iframeRef?.current;
    const iframeDoc = iframe?.contentDocument;
    if (iframeDoc) {
      const iframeSel = iframeDoc.getSelection();
      const text = readDomSelectionText(iframeSel);
      if (text && iframeSel && iframeSel.rangeCount > 0) {
        applySelection(iframeSel, iframeSel.getRangeAt(0).getBoundingClientRect(), panel);
        return;
      }
    }
    const domSel = window.getSelection();
    const text = readDomSelectionText(domSel);
    if (!text || !domSel || domSel.rangeCount === 0) {
      if (!composing) clear();
      return;
    }
    const range = domSel.getRangeAt(0);
    if (!selectionInside(panel, range.commonAncestorContainer)) {
      if (!composing) clear();
      return;
    }
    applySelection(domSel, range.getBoundingClientRect(), panel);
  }, [applySelection, clear, composing, iframeRef, panelRef]);

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

    const iframe = iframeRef?.current;
    let iframeDoc: Document | null = null;
    const bindIframe = () => {
      iframeDoc = iframe?.contentDocument ?? null;
      if (!iframeDoc) return;
      iframeDoc.addEventListener('mouseup', onPointerUp);
      iframeDoc.addEventListener('keyup', onKeyUp);
      iframeDoc.addEventListener('selectionchange', refreshFromDom);
    };
    bindIframe();
    iframe?.addEventListener('load', bindIframe);

    return () => {
      panel.removeEventListener('mouseup', onPointerUp);
      panel.removeEventListener('keyup', onKeyUp);
      document.removeEventListener('selectionchange', refreshFromDom);
      iframe?.removeEventListener('load', bindIframe);
      if (iframeDoc) {
        iframeDoc.removeEventListener('mouseup', onPointerUp);
        iframeDoc.removeEventListener('keyup', onKeyUp);
        iframeDoc.removeEventListener('selectionchange', refreshFromDom);
      }
    };
  }, [iframeRef, panelRef, refreshFromDom]);

  useEffect(() => {
    clear();
  }, [clear, kind, path, title]);

  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      const floatEl = document.querySelector('[data-testid="artifact-selection-float"]');
      if (floatEl && target && floatEl.contains(target)) return;
      // Dismiss on any outside click; a new selection's mouseup will reopen the float.
      clear();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') clear();
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [clear, iframeRef, panelRef]);

  if (!float) return null;

  const showStyle = Boolean(path.trim() && isPreviewStyleEditable(kind) && onStyleAction);

  const send = () => {
    submitPreviewAiEdit(float.sel, instruction);
    window.getSelection()?.removeAllRanges();
    try {
      iframeRef?.current?.contentDocument?.getSelection()?.removeAllRanges();
    } catch {
      /* ignore */
    }
    clear();
  };

  const styleBtn = (action: SelectionStyleAction, label: ReactNode, testId: string, className = '') => (
    <button
      type="button"
      className={`inline-flex h-7 w-7 items-center justify-center rounded text-xs text-text hover:bg-secondary ${className}`}
      data-testid={testId}
      onClick={() => onStyleAction?.(action, float.sel.preview)}
    >
      {label}
    </button>
  );

  return (
    <div
      className="artifact-selection-float fixed z-50 flex -translate-x-1/2 flex-col gap-1.5 rounded-full border border-border bg-background px-2 py-1 shadow-md"
      style={{ top: float.top, left: float.left }}
      data-testid="artifact-selection-float"
      onMouseDown={event => event.preventDefault()}
    >
      <div className="flex items-center gap-0.5">
        <button
          type="button"
          className="inline-flex h-7 items-center gap-1 rounded-full px-2.5 text-xs font-medium text-text-link hover:bg-secondary"
          data-testid="artifact-ai-edit-btn"
          onClick={() => setComposing(true)}
        >
          <Sparkles size={14} aria-hidden="true" />
          {t('artifacts.aiEdit')}
        </button>
        {showStyle ? (
          <>
            <span className="mx-1 h-4 w-px shrink-0 bg-border" aria-hidden="true" />
            {styleBtn('bold', <span className="font-bold">B</span>, 'artifact-style-bold')}
            {styleBtn('italic', <span className="italic">I</span>, 'artifact-style-italic')}
            {styleBtn('underline', <span className="underline">U</span>, 'artifact-style-underline')}
            {styleBtn('strike', <span className="line-through">S</span>, 'artifact-style-strike')}
            {styleBtn('link', <Link2 size={14} />, 'artifact-style-link')}
            {styleBtn('code', <span className="font-mono text-[11px]">{'</>'}</span>, 'artifact-style-code')}
          </>
        ) : null}
      </div>
      {composing ? (
        <div className="flex min-w-[240px] items-center gap-1 rounded-full border border-border bg-background px-2 py-1">
          <input
            className="min-w-0 flex-1 border-0 bg-transparent px-1 py-0.5 text-xs text-text outline-none"
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
            className="shrink-0 rounded-full bg-accent px-2.5 py-1 text-xs text-text"
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
