import { createPortal } from 'react-dom';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FileArchive, Info, Loader2, X } from 'lucide-react';
import UpFileIcon from '../../assets/upFile.svg?react';
import {
  DESKTOP_FILE_DRAG_EVENT,
  registerDesktopLocalFilesConsumer,
  selectLocalFiles,
  type LocalFilePick,
} from '../../features/workspace/localFilePicker';
import { isAgentUploadFilename } from '../../features/agentManagement';
import { useDesktopLocalFilePickerReady } from '../../hooks';

type AgentUploadDialogProps = {
  error?: string | null;
  onCancel: () => void;
  onConfirm: (path: string) => void | Promise<void>;
};

const DROP_ACCEPT_WINDOW_MS = 1200;

function formatFileSize(bytes: number): string {
  if (bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const unitIndex = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const size = bytes / Math.pow(1024, unitIndex);
  return `${size.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function pickFromDroppedFile(file: File & { path?: string }): LocalFilePick | undefined {
  const path = typeof file.path === 'string' ? file.path.trim() : '';
  if (!path) return undefined;
  return {
    path,
    filename: file.name,
    size: file.size,
    mime_type: file.type || 'application/octet-stream',
    kind: 'document',
  };
}

export function AgentUploadDialog({ error, onCancel, onConfirm }: AgentUploadDialogProps) {
  const { t } = useTranslation();
  const [filePick, setFilePick] = useState<LocalFilePick | null>(null);
  const [pickerError, setPickerError] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [browsing, setBrowsing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const desktopReady = useDesktopLocalFilePickerReady();
  const dialogRef = useRef<HTMLElement>(null);
  const dropAcceptUntilRef = useRef(0);
  const lastDropIdRef = useRef<string | null>(null);

  const acceptPick = useCallback((pick: LocalFilePick | undefined) => {
    if (!pick) return;
    if (!isAgentUploadFilename(pick.filename)) {
      setFilePick(null);
      setPickerError(t('agentManagement.form.uploadInvalidType'));
      return;
    }
    setPickerError(null);
    setFilePick(pick);
  }, [t]);

  const handleBrowse = async () => {
    if (browsing || submitting || filePick) return;
    setBrowsing(true);
    try {
      const result = await selectLocalFiles(false);
      if (result.ok) {
        acceptPick(result.files[0]);
      } else if (result.reason === 'unsupported') {
        setPickerError(t('agentManagement.form.uploadPickerUnsupported'));
      } else if (result.reason === 'failed') {
        setPickerError(result.message || t('agentManagement.form.uploadPickerFailed'));
      }
    } finally {
      setBrowsing(false);
    }
  };

  useEffect(() => {
    if (!desktopReady) return undefined;
    const unregister = registerDesktopLocalFilesConsumer((detail, files) => {
      if (detail?.source && detail.source !== 'drop') return;
      if (!files.length) return;
      const dropId = typeof detail?.dropId === 'string' ? detail.dropId : null;
      if (dropId && lastDropIdRef.current === dropId) return;
      const hasCoordinates = typeof detail?.clientX === 'number' && typeof detail?.clientY === 'number';
      const inDropZone = hasCoordinates
        ? Boolean(document.elementFromPoint(detail.clientX as number, detail.clientY as number)?.closest('.agent-management-upload-picker'))
        : false;
      const trusted = detail?.trusted === true;
      const acceptByTime = Date.now() <= dropAcceptUntilRef.current;
      if (!trusted && !acceptByTime && !inDropZone) return;
      if (dropId) lastDropIdRef.current = dropId;
      setDragActive(false);
      acceptPick(files[0]);
    });
    return unregister;
  }, [acceptPick, desktopReady]);

  useEffect(() => {
    const onFileDrag = (event: Event) => {
      const active = Boolean((event as CustomEvent<{ active?: boolean }>).detail?.active);
      if (active) dropAcceptUntilRef.current = Date.now() + DROP_ACCEPT_WINDOW_MS;
    };
    window.addEventListener(DESKTOP_FILE_DRAG_EVENT, onFileDrag as EventListener);
    return () => window.removeEventListener(DESKTOP_FILE_DRAG_EVENT, onFileDrag as EventListener);
  }, []);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return undefined;
    const focusableSelector = 'button:not([disabled]), [tabindex]:not([tabindex="-1"])';
    const focusable = () => Array.from(dialog.querySelectorAll<HTMLElement>(focusableSelector));
    dialog.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        if (!submitting) onCancel();
        return;
      }
      if (event.key !== 'Tab') return;
      const elements = focusable();
      if (!elements.length) return;
      const first = elements[0];
      const last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onCancel, submitting]);

  const handleConfirm = async () => {
    if (!filePick || submitting) return;
    setSubmitting(true);
    try {
      await onConfirm(filePick.path);
    } finally {
      setSubmitting(false);
    }
  };

  return createPortal(
    <div
      className="agent-management-selection-overlay"
      role="presentation"
      onMouseDown={event => {
        if (event.target === event.currentTarget && !submitting) onCancel();
      }}
    >
      <section
        ref={dialogRef}
        className="agent-management-upload-dialog"
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        aria-labelledby="agent-upload-dialog-title"
        aria-describedby="agent-upload-dialog-hint"
        data-testid="agent-management-upload-dialog"
        onMouseDown={event => event.stopPropagation()}
      >
        <header>
          <h2 id="agent-upload-dialog-title" data-testid="agent-management-upload-dialog-title">{t('agentManagement.actions.createByUpload')}</h2>
          <button type="button" onClick={onCancel} aria-label={t('common.close')} disabled={submitting} data-testid="agent-management-upload-dialog-close-btn">
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <p id="agent-upload-dialog-hint" className="agent-management-upload-dialog__hint" data-testid="agent-management-upload-dialog-hint">
          <Info size={14} aria-hidden="true" />
          <span>{t('agentManagement.form.uploadHint')}</span>
        </p>

        <div
          className={`agent-management-upload-picker${dragActive ? ' is-dragging' : ''}${pickerError ? ' has-error' : ''}`}
          role="button"
          tabIndex={filePick || submitting ? -1 : 0}
          aria-label={t('agentManagement.form.uploadPlaceholder')}
          data-testid="agent-management-upload-dialog-picker"
          onKeyDown={event => {
            if ((event.key === 'Enter' || event.key === ' ') && !filePick && !submitting) {
              event.preventDefault();
              void handleBrowse();
            }
          }}
          onClick={() => void handleBrowse()}
          onDragOver={event => {
            if (!Array.from(event.dataTransfer.types).includes('Files')) return;
            event.preventDefault();
            if (!desktopReady) {
              event.dataTransfer.dropEffect = 'none';
              return;
            }
            event.dataTransfer.dropEffect = 'copy';
            setDragActive(true);
          }}
          onDragLeave={() => setDragActive(false)}
          onDrop={event => {
            if (!Array.from(event.dataTransfer.types).includes('Files')) return;
            event.preventDefault();
            setDragActive(false);
            const file = event.dataTransfer.files[0] as (File & { path?: string }) | undefined;
            acceptPick(file ? pickFromDroppedFile(file) : undefined);
          }}
        >
          {filePick ? (
            <div className="agent-management-upload-picker__selected" data-testid="agent-management-upload-dialog-selected-file">
              <FileArchive size={22} aria-hidden="true" />
              <div>
                <p title={filePick.filename}>{filePick.filename}</p>
                <small>{formatFileSize(filePick.size)}</small>
              </div>
              <button
                type="button"
                aria-label={t('agentManagement.form.removeUpload')}
                data-testid="agent-management-upload-dialog-remove-btn"
                onClick={event => {
                  event.stopPropagation();
                  setFilePick(null);
                  setPickerError(null);
                }}
                disabled={submitting}
              >
                <X size={16} aria-hidden="true" />
              </button>
            </div>
          ) : browsing ? (
            <Loader2 size={22} aria-hidden="true" className="agent-management-upload-picker__spinner" />
          ) : (
            <>
              <UpFileIcon aria-hidden="true" />
              <span>{t('agentManagement.form.uploadPlaceholder')}</span>
            </>
          )}
        </div>
        {error || pickerError ? <p className="agent-management-upload-dialog__error" role="alert" data-testid="agent-management-upload-dialog-error">{error || pickerError}</p> : null}

        <footer>
          <button type="button" className="agent-management-button agent-management-button--secondary" onClick={onCancel} disabled={submitting} data-testid="agent-management-upload-dialog-cancel-btn">
            {t('common.cancel')}
          </button>
          <button type="button" className="agent-management-button agent-management-button--primary" onClick={() => void handleConfirm()} disabled={!filePick || submitting} data-testid="agent-management-upload-dialog-confirm-btn">
            {submitting ? t('agentManagement.actions.uploading') : t('common.confirm')}
          </button>
        </footer>
      </section>
    </div>,
    document.body,
  );
}
