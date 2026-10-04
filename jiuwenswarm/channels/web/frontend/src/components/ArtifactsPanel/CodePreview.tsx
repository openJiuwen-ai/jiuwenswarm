import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { defaultHighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { EditorState } from '@codemirror/state';
import { EditorView, lineNumbers } from '@codemirror/view';
import { LoaderCircle } from 'lucide-react';
import { loadCodeLanguage } from './codeLanguages';

const lightPreviewTheme = EditorView.theme(
  {
    '&': {
      height: '100%',
      backgroundColor: 'transparent',
      fontSize: '12px',
    },
    '.cm-scroller': {
      overflow: 'auto',
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
    },
    '.cm-content': {
      padding: '0',
    },
    '.cm-gutters': {
      backgroundColor: 'transparent',
      border: 'none',
    },
    '.cm-lineNumbers .cm-gutterElement': {
      minWidth: '32px',
      padding: '0 10px 0 4px',
    },
    '.cm-focused': {
      outline: 'none',
    },
  },
  { dark: false },
);

type LoadState = 'loading' | 'ready' | 'error';

export function CodePreview({
  content,
  name,
  mimeType,
  editable = false,
  onChange,
}: {
  content: string;
  name: string;
  mimeType?: string;
  editable?: boolean;
  onChange?: (value: string) => void;
}) {
  const { t } = useTranslation();
  const hostRef = useRef<HTMLDivElement>(null);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  const [loadState, setLoadState] = useState<LoadState>('loading');

  useEffect(() => {
    let disposed = false;
    let view: EditorView | null = null;
    setLoadState('loading');

    void loadCodeLanguage(name, mimeType)
      .then(language => {
        if (disposed || !hostRef.current) return;
        view = new EditorView({
          parent: hostRef.current,
          state: EditorState.create({
            doc: content,
            extensions: [
              EditorState.readOnly.of(!editable),
              EditorView.editable.of(editable),
              EditorView.contentAttributes.of({
                'aria-label': name,
                tabindex: '0',
              }),
              lineNumbers(),
              syntaxHighlighting(defaultHighlightStyle),
              language,
              lightPreviewTheme,
              EditorView.updateListener.of(update => {
                if (update.docChanged) {
                  onChangeRef.current?.(update.state.doc.toString());
                }
              }),
            ],
          }),
        });
        setLoadState('ready');
      })
      .catch(() => {
        if (!disposed) setLoadState('error');
      });

    return () => {
      disposed = true;
      view?.destroy();
    };
    // Omit `content` from deps: parent draft updates on every keystroke would remount and steal focus.
    // Artifact/mode changes still remount via name/mimeType/editable (and parent keys).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editable, mimeType, name]);

  return (
    <div className="relative h-full min-h-0 w-full overflow-hidden" data-testid="artifact-code-preview">
      <div ref={hostRef} className="h-full min-h-0 w-full" />
      {loadState === 'loading' && (
        <div className="absolute inset-0 flex items-center justify-center gap-2 text-sm text-text-muted" data-testid="artifact-code-preview-overlay" data-variant="loading">
          <LoaderCircle className="animate-spin" size={16} />
          {t('common.loading')}
        </div>
      )}
      {loadState === 'error' && (
        <div className="absolute inset-0 flex items-center justify-center text-sm text-text-muted" data-testid="artifact-code-preview-overlay" data-variant="error">
          {t('artifacts.previewFailed')}
        </div>
      )}
    </div>
  );
}
