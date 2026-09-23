import { createPortal } from 'react-dom';
import { useCallback, useEffect, useRef, useState } from 'react';
import { FileArchive, Info, Loader2, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import UpFileIcon from '../../assets/upFile.svg?react';
import {
  DESKTOP_DIRECTORY_DROP_REJECTED_EVENT,
  DESKTOP_FILE_DRAG_EVENT,
  registerDesktopLocalFilesConsumer,
  selectLocalFiles,
  type LocalFilePick,
} from '../../features/workspace/localFilePicker';
import { isAgentGroupUploadFilename, isAgentUploadFilename } from '../../features/agentManagement';
import { useDesktopLocalFilePickerReady } from '../../hooks';
import { useDialogFocusTrap } from './useDialogFocusTrap';

type AgentGroupUploadDialogProps = {
  initialKind?: 'agent' | 'group';
  error?: string | null;
  onCancel: () => void;
  onConfirm: (path: string, kind: 'agent' | 'group') => void | Promise<void>;
};

const DROP_ACCEPT_WINDOW_MS = 1200;

function formatFileSize(bytes: number): string {
  if (bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const size = bytes / Math.pow(1024, index);
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
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

export function DefinitionUploadDialog({
  initialKind = 'group',
  error,
  onCancel,
  onConfirm,
}: AgentGroupUploadDialogProps) {
  const { t } = useTranslation();
  const [kind, setKind] = useState<'agent' | 'group'>(initialKind);
  const [filePick, setFilePick] = useState<LocalFilePick | null>(null);
  const [pickerError, setPickerError] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [browsing, setBrowsing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const desktopReady = useDesktopLocalFilePickerReady();
  const dialogRef = useRef<HTMLElement>(null);
  const dropAcceptUntilRef = useRef(0);
  const lastDropIdRef = useRef<string | null>(null);

  const acceptPick = useCallback(
    (pick: LocalFilePick | undefined) => {
      if (!pick) return;
      const valid = kind === 'group' ? isAgentGroupUploadFilename(pick.filename) : isAgentUploadFilename(pick.filename);
      if (!valid) {
        setFilePick(null);
        setPickerError(
          t(
            kind === 'group'
              ? 'agentManagement.group.form.uploadInvalidType'
              : 'agentManagement.form.uploadInvalidType',
          ),
        );
        return;
      }
      setPickerError(null);
      setFilePick(pick);
    },
    [kind, t],
  );

  const handleKindChange = (nextKind: 'agent' | 'group') => {
    setKind(nextKind);
    setFilePick(null);
    setPickerError(null);
  };

  const handleBrowse = async () => {
    if (browsing || submitting || filePick) return;
    setBrowsing(true);
    try {
      const result = await selectLocalFiles(false);
      if (result.ok) acceptPick(result.files[0]);
      else if (result.reason === 'unsupported') setPickerError(t('agentManagement.form.uploadPickerUnsupported'));
      else if (result.reason === 'failed')
        setPickerError(result.message || t('agentManagement.form.uploadPickerFailed'));
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
        ? Boolean(
            document
              .elementFromPoint(detail.clientX as number, detail.clientY as number)
              ?.closest('.agent-management-upload-picker'),
          )
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
    const rejectDirectory = () => {
      setDragActive(false);
      setFilePick(null);
      setPickerError(t('agentManagement.form.uploadDirectoryUnsupported'));
    };
    window.addEventListener(DESKTOP_DIRECTORY_DROP_REJECTED_EVENT, rejectDirectory);
    return () => window.removeEventListener(DESKTOP_DIRECTORY_DROP_REJECTED_EVENT, rejectDirectory);
  }, [t]);

  useDialogFocusTrap({ dialogRef, onEscape: onCancel, escapeDisabled: submitting });

  const handleConfirm = async () => {
    if (!filePick || submitting) return;
    setSubmitting(true);
    try {
      await onConfirm(filePick.path, kind);
    } finally {
      setSubmitting(false);
    }
  };

  const typeHint = kind === 'group' ? t('agentManagement.group.form.uploadHint') : t('agentManagement.form.uploadHint');
  const uploadPlaceholder = t(
    desktopReady
      ? 'agentManagement.form.uploadPlaceholder'
      : 'agentManagement.form.webUploadPlaceholder',
  );
  return createPortal(
    <div
      className="agent-management-selection-overlay agent-group-upload-dialog-overlay"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !submitting) onCancel();
      }}
    >
      <section
        ref={dialogRef}
        className="agent-management-upload-dialog agent-group-upload-dialog"
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        aria-labelledby="agent-group-upload-dialog-title"
        onMouseDown={(event) => event.stopPropagation()}
        data-testid="agent-group-upload-dialog"
      >
        <header>
          <h2 id="agent-group-upload-dialog-title">{t('agentManagement.actions.createByUpload')}</h2>
          <button
            type="button"
            onClick={onCancel}
            aria-label={t('common.close')}
            disabled={submitting}
            data-testid="agent-group-upload-dialog-close"
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>
        <p className="agent-management-upload-dialog__hint" data-testid="agent-management-group-upload-dialog-hint">
          <Info size={14} aria-hidden="true" />
          <span className="agent-management-upload-dialog__hint-copy">{typeHint}</span>
        </p>
        <div
          className="agent-group-upload-dialog__kind"
          role="tablist"
          aria-label={t('agentManagement.group.form.uploadKindLabel')}
        >
          <span className="agent-group-upload-dialog__label">{t('agentManagement.group.form.uploadKindLabel')}</span>
          <div className="agent-group-upload-dialog__options">
            <button
              type="button"
              role="tab"
              aria-selected={kind === 'agent'}
              className={kind === 'agent' ? 'is-active' : ''}
              data-testid="agent-group-upload-agent-tab"
              onClick={() => handleKindChange('agent')}
            >
              <span className="agent-group-upload-dialog__radio" aria-hidden="true" />
              {t('agentManagement.group.form.uploadAgentTab')}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={kind === 'group'}
              className={kind === 'group' ? 'is-active' : ''}
              data-testid="agent-group-upload-group-tab"
              onClick={() => handleKindChange('group')}
            >
              <span className="agent-group-upload-dialog__radio" aria-hidden="true" />
              {t('agentManagement.group.form.uploadGroupTab')}
            </button>
          </div>
        </div>
        <div
          className={`agent-management-upload-picker${dragActive ? ' is-dragging' : ''}${pickerError ? ' has-error' : ''}`}
          data-testid="agent-group-upload-picker"
          role={filePick ? undefined : 'button'}
          tabIndex={filePick || submitting ? -1 : 0}
          aria-label={filePick ? undefined : uploadPlaceholder}
          onKeyDown={(event) => {
            if ((event.key === 'Enter' || event.key === ' ') && !filePick && !submitting) {
              event.preventDefault();
              void handleBrowse();
            }
          }}
          onClick={() => {
            if (!filePick) void handleBrowse();
          }}
          onDragOver={(event) => {
            if (filePick || !Array.from(event.dataTransfer.types).includes('Files')) return;
            event.preventDefault();
            if (!desktopReady) {
              event.dataTransfer.dropEffect = 'none';
              return;
            }
            event.dataTransfer.dropEffect = 'copy';
            setDragActive(true);
          }}
          onDragLeave={() => setDragActive(false)}
          onDrop={(event) => {
            if (filePick || !Array.from(event.dataTransfer.types).includes('Files')) return;
            event.preventDefault();
            setDragActive(false);
            const file = event.dataTransfer.files[0] as (File & { path?: string }) | undefined;
            acceptPick(file ? pickFromDroppedFile(file) : undefined);
          }}
        >
          {filePick ? (
            <div className="agent-management-upload-picker__selected">
              <FileArchive size={22} aria-hidden="true" />
              <div>
                <p title={filePick.filename}>{filePick.filename}</p>
                <small>{formatFileSize(filePick.size)}</small>
              </div>
              <button
                type="button"
                aria-label={t('agentManagement.form.removeUpload')}
                data-testid="agent-group-upload-remove-file"
                onClick={(event) => {
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
              <span>{uploadPlaceholder}</span>
            </>
          )}
        </div>
        {pickerError || error ? (
          <p className="agent-management-upload-dialog__error" role="alert">
            {pickerError || error}
          </p>
        ) : null}
        <footer>
          <button
            type="button"
            className="agent-management-button agent-management-button--secondary"
            data-testid="agent-group-upload-cancel"
            onClick={onCancel}
            disabled={submitting}
          >
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="agent-management-button agent-management-button--primary"
            data-testid="agent-group-upload-confirm"
            onClick={() => void handleConfirm()}
            disabled={!filePick || submitting}
          >
            {submitting ? t('agentManagement.actions.uploading') : t('common.confirm')}
          </button>
        </footer>
      </section>
    </div>,
    document.body,
  );
}
