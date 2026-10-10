import { useCallback, useEffect, useState } from 'react';
import { ChevronLeft, Folder, FolderPlus, Loader2, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  createServerDirectory,
  listServerDirectories,
  selectServerDirectory,
  type ProjectDirectoryPickResult,
  type ServerDirectoryListing,
} from './projectDirectoryPicker';
import './ServerDirectoryPickerDialog.css';

type ServerDirectoryPickerDialogProps = {
  initialPath?: string;
  onCancel: () => void;
  onSelect: (result: Extract<ProjectDirectoryPickResult, { ok: true }>) => void;
};

/** Server-side directory browser backed by canonicalized Gateway path RPCs. */
export function ServerDirectoryPickerDialog({ initialPath, onCancel, onSelect }: ServerDirectoryPickerDialogProps) {
  const { t } = useTranslation();
  const [listing, setListing] = useState<ServerDirectoryListing | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [newDirectoryName, setNewDirectoryName] = useState('');

  const load = useCallback(async (path?: string) => {
    setLoading(true);
    setError('');
    try {
      setListing(await listServerDirectories(path));
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : String(loadError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(initialPath);
  }, [initialPath, load]);

  async function createDirectory() {
    if (!listing || !newDirectoryName.trim()) return;
    const result = await createServerDirectory(listing.path, newDirectoryName.trim());
    if (!result.ok) {
      setError(result.message || t('multiSession.project.directoryCreateFailed'));
      return;
    }
    setNewDirectoryName('');
    await load(result.path);
  }

  async function confirmSelection() {
    if (!listing) return;
    const result = await selectServerDirectory(listing.path);
    if (!result.ok) {
      setError(result.message || t('multiSession.project.directoryPickerFailed'));
      return;
    }
    onSelect(result);
  }

  return (
    <div className="server-directory-picker__backdrop" role="presentation">
      <section
        className="server-directory-picker"
        role="dialog"
        aria-modal="true"
        aria-labelledby="server-directory-picker-title"
        data-testid="workspace-directory-picker"
      >
        <header className="server-directory-picker__header">
          <h2 id="server-directory-picker-title">{t('multiSession.project.directoryBrowserTitle')}</h2>
          <button
            data-testid="workspace-directory-close"
            type="button"
            onClick={onCancel}
            aria-label={t('common.close')}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <div className="server-directory-picker__roots" aria-label={t('multiSession.project.directoryRoots')}>
          {listing?.roots.map((root) => (
            <button
              data-testid="workspace-directory-root"
              key={root.path}
              type="button"
              onClick={() => void load(root.path)}
              title={root.path}
            >
              <Folder size={15} aria-hidden="true" />
              <span>{root.name}</span>
            </button>
          ))}
        </div>

        <div className="server-directory-picker__path">
          <button
            data-testid="workspace-directory-parent"
            type="button"
            disabled={!listing?.parent_path || loading}
            onClick={() => void load(listing?.parent_path)}
            aria-label={t('multiSession.project.directoryParent')}
          >
            <ChevronLeft size={17} aria-hidden="true" />
          </button>
          <span dir="ltr" title={listing?.path}>
            {listing?.path || t('multiSession.project.directoryLoading')}
          </span>
        </div>

        <div className="server-directory-picker__list">
          {loading ? (
            <div className="server-directory-picker__status">
              <Loader2 className="is-spinning" size={18} />
              {t('multiSession.project.directoryLoading')}
            </div>
          ) : null}
          {!loading && listing?.entries.length === 0 ? (
            <div className="server-directory-picker__status">{t('multiSession.project.directoryEmpty')}</div>
          ) : null}
          {!loading &&
            listing?.entries.map((entry) => (
              <button
                data-testid="workspace-directory-entry"
                key={entry.path}
                type="button"
                onClick={() => void load(entry.path)}
                title={entry.path}
              >
                <Folder size={17} aria-hidden="true" />
                <span>{entry.name}</span>
              </button>
            ))}
        </div>

        <div className="server-directory-picker__create">
          <FolderPlus size={17} aria-hidden="true" />
          <input
            data-testid="workspace-directory-new-name"
            value={newDirectoryName}
            onChange={(event) => setNewDirectoryName(event.target.value)}
            placeholder={t('multiSession.project.directoryNewName')}
            maxLength={255}
          />
          <button
            data-testid="workspace-directory-create"
            type="button"
            disabled={!listing || !newDirectoryName.trim() || loading}
            onClick={() => void createDirectory()}
          >
            {t('multiSession.project.directoryCreate')}
          </button>
        </div>

        {error ? (
          <div className="server-directory-picker__error" role="alert">
            {error}
          </div>
        ) : null}
        {listing?.truncated ? (
          <div className="server-directory-picker__notice">{t('multiSession.project.directoryTruncated')}</div>
        ) : null}

        <footer className="server-directory-picker__actions">
          <button data-testid="workspace-directory-cancel" type="button" onClick={onCancel}>
            {t('multiSession.project.cancel')}
          </button>
          <button
            data-testid="workspace-directory-select"
            type="button"
            className="is-primary"
            disabled={!listing || loading}
            onClick={() => void confirmSelection()}
          >
            {t('multiSession.project.selectCurrentDirectory')}
          </button>
        </footer>
      </section>
    </div>
  );
}
