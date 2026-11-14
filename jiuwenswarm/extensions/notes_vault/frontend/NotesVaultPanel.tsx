/**
 * Presentation-only panel for the Notes Vault plugin.
 *
 * Holds no RPC concerns: every action is a callback supplied by `index.tsx`,
 * which keeps this file trivially testable and free of core-frontend imports
 * beyond React.
 */
import type { ChangeEvent, FormEvent } from 'react';

export interface Note {
  id: string;
  title: string;
  body: string;
  created_at: string;
  updated_at: string;
  tags: string[];
  pinned: boolean;
}

export interface NoteDraft {
  title: string;
  body: string;
  tags: string;
  pinned: boolean;
}

export interface VaultStats {
  scope: string;
  notes: number;
  pinned: number;
  tags: number;
  max_notes: number;
}

export interface NotesVaultPanelProps {
  notes: Note[];
  stats: VaultStats | null;
  selectedId: string | null;
  draft: NoteDraft;
  query: string;
  loading: boolean;
  error: string | null;
  onSelect: (note: Note) => void;
  onNew: () => void;
  onSave: () => void;
  onDelete: () => void;
  onExport: () => void;
  onSearch: (query: string) => void;
  onDraftChange: (draft: NoteDraft) => void;
}

export function NotesVaultPanel({
  notes,
  stats,
  selectedId,
  draft,
  query,
  loading,
  error,
  onSelect,
  onNew,
  onSave,
  onDelete,
  onExport,
  onSearch,
  onDraftChange,
}: NotesVaultPanelProps) {
  const edit = (patch: Partial<NoteDraft>) => onDraftChange({ ...draft, ...patch });
  const onText = (key: 'title' | 'tags') => (event: ChangeEvent<HTMLInputElement>) =>
    edit({ [key]: event.target.value } as Partial<NoteDraft>);

  return (
    <div className="notes-vault" data-testid="notes-vault">
      <header className="notes-vault-header">
        <h2 data-testid="notes-vault-title">Notes Vault</h2>
        <div className="notes-vault-actions">
          <input
            type="search"
            value={query}
            placeholder="Search notes"
            onChange={(event: ChangeEvent<HTMLInputElement>) => onSearch(event.target.value)}
            data-testid="notes-vault-search"
          />
          <button type="button" onClick={onNew} data-testid="notes-vault-new">New</button>
          <button type="button" onClick={onExport} data-testid="notes-vault-export">Export</button>
        </div>
      </header>

      {stats ? (
        <p className="notes-vault-stats" data-testid="notes-vault-stats">
          {stats.notes} / {stats.max_notes} notes, {stats.pinned} pinned, {stats.tags} tags in
          scope: {stats.scope}
        </p>
      ) : null}

      {error ? (
        <p className="notes-vault-error" data-testid="notes-vault-error">{error}</p>
      ) : null}

      <div className="notes-vault-body">
        <ul className="notes-vault-list" data-testid="notes-vault-list">
          {notes.length === 0 && !loading ? (
            <li className="notes-vault-empty">No notes yet</li>
          ) : null}
          {notes.map((note) => (
            <li key={note.id}>
              <button
                type="button"
                className={note.id === selectedId ? 'is-selected' : undefined}
                onClick={() => onSelect(note)}
                data-testid={`notes-vault-item-${note.id}`}
              >
                <span className="notes-vault-item-title">
                  {note.pinned ? '[pin] ' : ''}
                  {note.title}
                </span>
                <span className="notes-vault-item-meta">{note.updated_at}</span>
              </button>
            </li>
          ))}
        </ul>

        <form
          className="notes-vault-editor"
          data-testid="notes-vault-editor"
          onSubmit={(event: FormEvent<HTMLFormElement>) => {
            event.preventDefault();
            onSave();
          }}
        >
          <label>
            <span>Title</span>
            <input
              type="text"
              value={draft.title}
              onChange={onText('title')}
              data-testid="notes-vault-field-title"
            />
          </label>
          <label>
            <span>Tags (comma separated)</span>
            <input
              type="text"
              value={draft.tags}
              onChange={onText('tags')}
              data-testid="notes-vault-field-tags"
            />
          </label>
          <label>
            <span>Body</span>
            <textarea
              rows={10}
              value={draft.body}
              onChange={(event: ChangeEvent<HTMLTextAreaElement>) =>
                edit({ body: event.target.value })
              }
              data-testid="notes-vault-field-body"
            />
          </label>
          <label className="notes-vault-pin">
            <input
              type="checkbox"
              checked={draft.pinned}
              onChange={(event: ChangeEvent<HTMLInputElement>) =>
                edit({ pinned: event.target.checked })
              }
              data-testid="notes-vault-field-pinned"
            />
            <span>Pin to top</span>
          </label>
          <div className="notes-vault-editor-actions">
            <button type="submit" disabled={loading} data-testid="notes-vault-save">
              {selectedId ? 'Save' : 'Create'}
            </button>
            <button
              type="button"
              onClick={onDelete}
              disabled={!selectedId}
              data-testid="notes-vault-delete"
            >
              Delete
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default NotesVaultPanel;
