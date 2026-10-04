import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react';
import { useTranslation } from 'react-i18next';
import { Link2, Sparkles } from 'lucide-react';
import { submitPreviewAiEdit } from '../../features/previewAiEditBridge';
import type { DocSelection } from './docSelection';
import type { PreviewKind } from './filePreviewModel';
import { filterCodeFenceLanguages } from './codeFenceLanguages';
import {
  buildDocSelection,
  floatingBarPosition,
  isPreviewStyleEditable,
  readDomSelectionText,
  supportsPreviewSelection,
} from './previewSelection';
import { countOccurrencesBeforeRange } from './previewTextEdit';
import type { TextWrapStyle } from './previewTextEdit';

export type SelectionStyleAction = TextWrapStyle;

export type StyleActionOptions = {
  linkUrl?: string;
  codeLanguage?: string;
};

export type StyleActionHandler = (
  action: SelectionStyleAction,
  selectedText: string,
  occurrenceIndex: number,
  options?: StyleActionOptions,
) => string | null | void;

export type StyleProbeHandler = (
  action: SelectionStyleAction,
  selectedText: string,
  occurrenceIndex: number,
) => boolean;

type SelectionFloatProps = {
  kind: PreviewKind;
  path: string;
  title: string;
  panelRef: RefObject<HTMLElement | null>;
  /** When set, also listen for selections inside this iframe document. */
  iframeRef?: RefObject<HTMLIFrameElement | null>;
  onStyleAction?: StyleActionHandler;
  /** Returns true when the style is already applied (used to toggle off without a panel). */
  onStyleProbe?: StyleProbeHandler;
  rangeHint?: string;
};

type FloatState = {
  sel: DocSelection;
  top: number;
  left: number;
  occurrenceIndex: number;
  /** Keep bar visible after a style click until the user clicks outside. */
  pinned: boolean;
};

type PanelMode = 'ai' | 'link' | 'code' | null;

function selectionInside(panel: HTMLElement, node: Node | null): boolean {
  if (!node) return false;
  const el = node.nodeType === Node.ELEMENT_NODE ? (node as Element) : node.parentElement;
  return Boolean(el && panel.contains(el));
}

export function SelectionFloat({
  kind,
  path = '',
  title = '',
  panelRef,
  iframeRef,
  onStyleAction,
  onStyleProbe,
  rangeHint,
}: SelectionFloatProps) {
  const { t } = useTranslation();
  const [float, setFloat] = useState<FloatState | null>(null);
  const [panelMode, setPanelMode] = useState<PanelMode>(null);
  const [instruction, setInstruction] = useState('');
  const [linkUrl, setLinkUrl] = useState('https://');
  const [codeQuery, setCodeQuery] = useState('');
  const [codeHighlight, setCodeHighlight] = useState(0);
  const floatRef = useRef(float);
  floatRef.current = float;
  const panelOpen = panelMode != null;

  const clear = useCallback(() => {
    setFloat(null);
    setPanelMode(null);
    setInstruction('');
    setLinkUrl('https://');
    setCodeQuery('');
    setCodeHighlight(0);
  }, []);

  const applySelection = useCallback(
    (domSel: Selection | null | undefined, clientRect: DOMRect | null, root: HTMLElement) => {
      if (!supportsPreviewSelection(kind)) {
        clear();
        return;
      }
      const text = readDomSelectionText(domSel);
      if (!text || !domSel || domSel.rangeCount === 0 || !clientRect) {
        if (!panelOpen && !floatRef.current?.pinned) clear();
        return;
      }
      const range = domSel.getRangeAt(0);
      const occurrenceIndex = countOccurrencesBeforeRange(root, range, text);
      const next = buildDocSelection({
        kind,
        path,
        title,
        selectedText: text,
        range: rangeHint,
      });
      if (!next) {
        if (!panelOpen && !floatRef.current?.pinned) clear();
        return;
      }
      const pos = floatingBarPosition(clientRect, root.getBoundingClientRect(), { barHalfWidth: 160 });
      setFloat({ sel: next, top: pos.top, left: pos.left, occurrenceIndex, pinned: false });
    },
    [clear, kind, panelOpen, path, rangeHint, title],
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
      if (!panelOpen && !floatRef.current?.pinned) clear();
      return;
    }
    const range = domSel.getRangeAt(0);
    if (!selectionInside(panel, range.commonAncestorContainer)) {
      if (!panelOpen && !floatRef.current?.pinned) clear();
      return;
    }
    applySelection(domSel, range.getBoundingClientRect(), panel);
  }, [applySelection, clear, iframeRef, panelOpen, panelRef]);

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
  }, [clear]);

  const codeOptions = useMemo(() => filterCodeFenceLanguages(codeQuery), [codeQuery]);

  if (!float) return null;

  const showStyle = Boolean(isPreviewStyleEditable(kind) && onStyleAction);

  const sendAi = () => {
    submitPreviewAiEdit(float.sel, instruction);
    window.getSelection()?.removeAllRanges();
    try {
      iframeRef?.current?.contentDocument?.getSelection()?.removeAllRanges();
    } catch {
      /* ignore */
    }
    clear();
  };

  const applyStyle = (action: SelectionStyleAction, options?: StyleActionOptions) => {
    const nextInner = onStyleAction?.(action, float.sel.preview, float.occurrenceIndex, options);
    if (typeof nextInner === 'string') {
      setFloat(current =>
        current
          ? {
              ...current,
              pinned: true,
              sel: { ...current.sel, preview: nextInner },
              occurrenceIndex: current.occurrenceIndex,
            }
          : current,
      );
      setPanelMode(null);
      setLinkUrl('https://');
      setCodeQuery('');
      setCodeHighlight(0);
    } else if (nextInner !== null) {
      setFloat(current => (current ? { ...current, pinned: true } : current));
    }
  };

  const onStyleClick = (action: SelectionStyleAction) => {
    if (action === 'link') {
      if (onStyleProbe?.('link', float.sel.preview, float.occurrenceIndex)) {
        applyStyle('link');
        return;
      }
      setPanelMode('link');
      setInstruction('');
      setCodeQuery('');
      return;
    }
    if (action === 'code') {
      if (onStyleProbe?.('code', float.sel.preview, float.occurrenceIndex)) {
        applyStyle('code');
        return;
      }
      setPanelMode('code');
      setInstruction('');
      setCodeQuery('');
      setCodeHighlight(0);
      return;
    }
    setPanelMode(null);
    applyStyle(action);
  };

  const confirmLink = () => {
    const url = linkUrl.trim();
    if (!url) return;
    applyStyle('link', { linkUrl: url });
  };

  const confirmCode = (language: string) => {
    applyStyle('code', { codeLanguage: language.trim() });
  };

  const styleBtn = (action: SelectionStyleAction, label: ReactNode, testId: string, className = '') => (
    <button
      type="button"
      className={`inline-flex h-7 w-7 items-center justify-center rounded-lg text-xs text-text hover:bg-secondary ${className}`}
      data-testid={testId}
      onClick={() => onStyleClick(action)}
    >
      {label}
    </button>
  );

  return (
    <div
      className="artifact-selection-float fixed z-50 flex -translate-x-1/2 flex-col gap-1 rounded-xl border border-border bg-card p-1.5 shadow-lg"
      style={{ top: float.top, left: float.left }}
      data-testid="artifact-selection-float"
      onMouseDown={event => event.preventDefault()}
    >
      <div className="flex items-center gap-0.5 px-0.5">
        <button
          type="button"
          className="inline-flex h-7 items-center gap-1 rounded-lg px-2.5 text-xs font-medium text-text-link hover:bg-secondary"
          data-testid="artifact-ai-edit-btn"
          onClick={() => {
            setPanelMode('ai');
            setCodeQuery('');
          }}
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
      {panelMode === 'ai' ? (
        <div className="flex min-w-[260px] items-center gap-1.5 rounded-lg bg-bg px-1.5 py-1">
          <input
            className="min-w-0 flex-1 rounded-md border-0 bg-transparent px-2 py-1 text-xs text-text outline-none placeholder:text-text-muted"
            data-testid="artifact-ai-edit-input"
            placeholder={t('artifacts.aiEditPlaceholder')}
            value={instruction}
            onChange={event => setInstruction(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter') {
                event.preventDefault();
                sendAi();
              }
              if (event.key === 'Escape') {
                event.preventDefault();
                setPanelMode(null);
                setInstruction('');
              }
            }}
            autoFocus
          />
          <button
            type="button"
            className="shrink-0 rounded-md bg-accent px-2.5 py-1 text-xs text-accent-foreground"
            data-testid="artifact-ai-edit-send"
            onClick={sendAi}
          >
            {t('artifacts.aiEditSend')}
          </button>
        </div>
      ) : null}
      {panelMode === 'link' ? (
        <div className="flex min-w-[260px] items-center gap-1.5 rounded-lg bg-bg px-1.5 py-1">
          <input
            className="min-w-0 flex-1 rounded-md border-0 bg-transparent px-2 py-1 text-xs text-text outline-none placeholder:text-text-muted"
            data-testid="artifact-link-url-input"
            placeholder={t('artifacts.linkUrlPlaceholder')}
            value={linkUrl}
            onChange={event => setLinkUrl(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter') {
                event.preventDefault();
                confirmLink();
              }
              if (event.key === 'Escape') {
                event.preventDefault();
                setPanelMode(null);
                setLinkUrl('https://');
              }
            }}
            autoFocus
          />
          <button
            type="button"
            className="shrink-0 rounded-md bg-accent px-2.5 py-1 text-xs text-accent-foreground"
            data-testid="artifact-link-url-confirm"
            onClick={confirmLink}
          >
            {t('artifacts.linkUrlConfirm')}
          </button>
        </div>
      ) : null}
      {panelMode === 'code' ? (
        <div className="flex min-w-[260px] flex-col gap-1 rounded-lg bg-bg px-1.5 py-1" data-testid="artifact-code-language-panel">
          <div className="flex items-center gap-1.5">
            <input
              className="min-w-0 flex-1 rounded-md border-0 bg-transparent px-2 py-1 text-xs text-text outline-none placeholder:text-text-muted"
              data-testid="artifact-code-language-input"
              role="combobox"
              aria-expanded="true"
              aria-controls="artifact-code-language-list"
              placeholder={t('artifacts.codeLanguagePlaceholder')}
              value={codeQuery}
              onChange={event => {
                setCodeQuery(event.target.value);
                setCodeHighlight(0);
              }}
              onKeyDown={event => {
                if (event.key === 'ArrowDown') {
                  event.preventDefault();
                  setCodeHighlight(current => Math.min(current + 1, Math.max(codeOptions.length - 1, 0)));
                } else if (event.key === 'ArrowUp') {
                  event.preventDefault();
                  setCodeHighlight(current => Math.max(current - 1, 0));
                } else if (event.key === 'Enter') {
                  event.preventDefault();
                  const picked = codeOptions[codeHighlight] ?? codeQuery.trim();
                  confirmCode(picked);
                } else if (event.key === 'Escape') {
                  event.preventDefault();
                  setPanelMode(null);
                  setCodeQuery('');
                }
              }}
              autoFocus
            />
            <button
              type="button"
              className="shrink-0 rounded-md bg-accent px-2.5 py-1 text-xs text-accent-foreground"
              data-testid="artifact-code-language-confirm"
              onClick={() => confirmCode(codeOptions[codeHighlight] ?? codeQuery.trim())}
            >
              {t('artifacts.codeLanguageConfirm')}
            </button>
          </div>
          <ul
            id="artifact-code-language-list"
            role="listbox"
            className="max-h-36 overflow-auto rounded-md border border-border bg-card py-0.5"
            data-testid="artifact-code-language-list"
          >
            <li>
              <button
                type="button"
                role="option"
                aria-selected={codeQuery.trim() === '' && codeHighlight < 0}
                className="flex w-full px-2 py-1 text-left text-xs text-text-muted hover:bg-secondary"
                data-testid="artifact-code-language-plain"
                onClick={() => confirmCode('')}
              >
                {t('artifacts.codeLanguagePlain')}
              </button>
            </li>
            {codeOptions.map((lang, index) => (
              <li key={lang}>
                <button
                  type="button"
                  role="option"
                  aria-selected={index === codeHighlight}
                  className={`flex w-full px-2 py-1 text-left font-mono text-xs hover:bg-secondary ${
                    index === codeHighlight ? 'bg-secondary text-text' : 'text-text'
                  }`}
                  data-testid={`artifact-code-language-option-${lang}`}
                  onMouseEnter={() => setCodeHighlight(index)}
                  onClick={() => confirmCode(lang)}
                >
                  {lang}
                </button>
              </li>
            ))}
            {codeOptions.length === 0 && codeQuery.trim() ? (
              <li>
                <button
                  type="button"
                  role="option"
                  aria-selected
                  className="flex w-full px-2 py-1 text-left font-mono text-xs text-text hover:bg-secondary"
                  data-testid="artifact-code-language-custom"
                  onClick={() => confirmCode(codeQuery.trim())}
                >
                  {codeQuery.trim()}
                </button>
              </li>
            ) : null}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
