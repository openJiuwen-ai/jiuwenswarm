import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { MarkdownRenderer } from '../../components/MarkdownRenderer';
import { DesignerStoryboardTable, DesignerStoryboardTableDraft } from './DesignerStoryboardTable';
import { saveDesignerTextFile } from './designerAssetUrl';
import { useDesignerGraphEditingLocked } from './useDesignerGraphEditingLocked';
import {
  DESIGNER_MATERIAL_SAVED_EVENT,
  isDesignerTableMaterial,
  isEditableDesignerMaterial,
  type DesignerMaterial,
} from './designerMaterials';
import { parseMarkdownTable } from './designerNodePreview';

type DesignerTextEditorProps = {
  material: DesignerMaterial;
  compact?: boolean;
  showStartButton?: boolean;
  startEditKey?: number;
  onEditingChange?: (editing: boolean) => void;
};

const EMPTY_EDITOR = { text: '', draft: '', editing: false, conflict: false };

export function DesignerTextEditor({
  material,
  compact = false,
  showStartButton = true,
  startEditKey = 0,
  onEditingChange,
}: DesignerTextEditorProps) {
  const editingLocked = useDesignerGraphEditingLocked();
  const { t } = useTranslation();
  const editable = isEditableDesignerMaterial(material);
  const isTable = isDesignerTableMaterial(material);
  const [{ text, draft, editing, conflict }, setEditor] = useState(EMPTY_EDITOR);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [reloadAt, setReloadAt] = useState(0);
  const onEditingChangeRef = useRef(onEditingChange);
  onEditingChangeRef.current = onEditingChange;
  const materialRef = useRef(material);
  materialRef.current = material;

  useEffect(() => {
    setEditor(EMPTY_EDITOR);
    setSaving(false);
    setError('');
    onEditingChangeRef.current?.(false);
  }, [material.id]);

  useEffect(() => {
    const onSaved = (event: Event) => {
      if ((event as CustomEvent<{ uri: string }>).detail?.uri !== material.uri) return;
      setEditor((current) => ({
        ...current,
        conflict: current.editing && current.draft !== current.text,
      }));
      setLoading(true);
      setReloadAt((value) => value + 1);
    };
    window.addEventListener(DESIGNER_MATERIAL_SAVED_EVENT, onSaved);
    return () => window.removeEventListener(DESIGNER_MATERIAL_SAVED_EVENT, onSaved);
  }, [material.uri]);

  useEffect(() => {
    if (!material.textUrl) return;
    let cancelled = false;
    const url = `${material.textUrl}${material.textUrl.includes('?') ? '&' : '?'}t=${reloadAt}`;
    setLoading(true);
    setError('');
    void fetch(url, { cache: 'no-store' })
      .then((response) => {
        if (!response.ok) throw new Error(String(response.status));
        return response.text();
      })
      .then((value) => {
        if (cancelled) return;
        setEditor((current) => {
          const dirty = current.editing && current.draft !== current.text;
          return {
            ...current,
            text: value,
            draft: dirty ? current.draft : value,
            conflict: dirty && current.draft !== value && (current.conflict || value !== current.text),
          };
        });
      })
      .catch(() => {
        if (!cancelled) setError(t('designer.materials.textError'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [material.id, material.textUrl, reloadAt, t]);

  const startEdit = () => {
    if (editingLocked) return;
    setEditor((current) => ({ ...current, draft: current.text, editing: true, conflict: false }));
    setError('');
    onEditingChange?.(true);
  };

  useEffect(() => {
    if (!startEditKey || !editable || editingLocked) return;
    startEdit();
    // Only re-run when the viewer asks to edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editingLocked, startEditKey]);

  const cancelEdit = () => {
    setEditor((current) => ({ ...current, draft: current.text, editing: false, conflict: false }));
    setError('');
    onEditingChange?.(false);
  };

  const saveEdit = async () => {
    setSaving(true);
    setError('');
    const isCurrent = () => materialRef.current.id === material.id && materialRef.current.uri === material.uri;
    try {
      await saveDesignerTextFile(material.uri, draft);
      if (isCurrent()) {
        setEditor({ text: draft, draft, editing: false, conflict: false });
        onEditingChangeRef.current?.(false);
      }
      window.dispatchEvent(new CustomEvent(DESIGNER_MATERIAL_SAVED_EVENT, { detail: { uri: material.uri } }));
    } catch (cause) {
      if (isCurrent()) setError(cause instanceof Error ? cause.message : t('designer.materials.saveFailed'));
    } finally {
      if (materialRef.current.id === material.id) setSaving(false);
    }
  };

  if (!material.textUrl)
    return <p data-testid="designer-text-placeholder">{t('designer.materials.placeholderHint')}</p>;
  if (error && !text && !editing) return <p data-testid="designer-text-error">{error}</p>;

  const onDraftKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      cancelEdit();
    }
    if ((event.ctrlKey || event.metaKey) && event.key === 's') {
      event.preventDefault();
      event.stopPropagation();
      if (!saving && !loading && !conflict && draft !== text) void saveEdit();
    }
  };
  const setDraft = (value: string) => {
    setEditor((current) => ({
      ...current,
      draft: value,
      conflict: current.conflict && value !== current.text,
    }));
  };
  const tableDraft = isTable && editing && parseMarkdownTable(draft) !== null;

  return (
    <div
      className={compact ? 'designer-text-editor designer-text-editor--compact' : 'designer-text-editor'}
      data-testid="designer-text-editor"
    >
      {editable && (editing || showStartButton) ? (
        <div className="designer-text-editor__toolbar">
          {editing ? (
            <>
              <button
                type="button"
                className="btn primary"
                disabled={saving || loading || draft === text}
                onClick={() => void saveEdit()}
                data-testid="designer-text-save"
                data-variant={saving ? 'saving' : conflict ? 'overwrite' : 'save'}
              >
                {saving
                  ? t('designer.materials.saving')
                  : t(conflict ? 'designer.materials.overwriteWithDraft' : 'designer.materials.save')}
              </button>
              <button
                type="button"
                className="btn"
                disabled={saving}
                onClick={cancelEdit}
                data-testid="designer-text-cancel"
              >
                {t('designer.materials.cancelEdit')}
              </button>
            </>
          ) : (
            <button
              type="button"
              className={`btn${editingLocked ? ' designer-edit-control' : ''}`}
              disabled={loading || editingLocked}
              onClick={startEdit}
              data-testid="designer-text-edit"
            >
              {t('designer.materials.edit')}
            </button>
          )}
        </div>
      ) : null}
      {conflict ? (
        <p role="status" data-testid="designer-text-conflict">
          {t('designer.materials.externalChange')}
        </p>
      ) : null}
      {tableDraft ? (
        <div onKeyDown={onDraftKeyDown}>
          <DesignerStoryboardTableDraft text={draft} disabled={saving} onChange={setDraft} />
        </div>
      ) : editing ? (
        <textarea
          className="designer-text-editor__textarea"
          value={draft}
          disabled={saving}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onDraftKeyDown}
          spellCheck={false}
          data-testid="designer-text-draft"
        />
      ) : isTable && !loading ? (
        <DesignerStoryboardTable
          text={text}
          testId="designer-text-preview"
          layout={compact ? 'node' : 'full'}
        />
      ) : (
        <MarkdownRenderer
          content={text}
          className="designer-text-editor__text chat-markdown"
          testId="designer-text-preview"
        />
      )}
      {error ? (
        <p className="designer-text-editor__error" data-testid="designer-text-error">
          {error}
        </p>
      ) : null}
    </div>
  );
}
