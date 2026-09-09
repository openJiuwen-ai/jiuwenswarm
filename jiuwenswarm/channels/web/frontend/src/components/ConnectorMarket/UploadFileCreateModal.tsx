import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X, UploadCloud, Info, FileArchive, Loader2 } from 'lucide-react';
import {
  selectLocalFiles,
  registerDesktopLocalFilesConsumer,
  DESKTOP_FILE_DRAG_EVENT,
  type LocalFilePick,
} from '../../features/workspace/localFilePicker';
import { useDesktopLocalFilePickerReady } from '../../hooks';
import { mapLocalPackageImportError } from '../../features/agentManagement/upload';

interface UploadFileCreateModalProps {
  error?: string | null;
  onCancel: () => void;
  onConfirm: (filePath: string) => void | Promise<void>;
}

const ACCEPTED_EXTENSIONS = ['.zip', '.tar'];
const DROP_ZONE_CLASS = 'plugin-upload-dropzone';
// 跟 ChatPanel/index.tsx 的 markDesktopFileDropZoneActive 同一套节流窗口：desktop_app.py 派发
// 的 drop 事件坐标不总是准，给命中判定留一小段宽限期。
const DROP_ACCEPT_WINDOW_MS = 1200;

function isAcceptedFilename(name: string): boolean {
  const lower = name.toLowerCase();
  return ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

function formatFileSize(bytes: number): string {
  if (bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  const size = bytes / Math.pow(1024, i);
  return `${size.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

// 对应高保真 3.5 上传文件创建。2026-08-20 用户给出真实接口 plugin_packages.import_local
// （{path, session_id}）——path 是后端要读的本地文件系统绝对路径，浏览器 <input type=file>/
// 原生 drag&drop 的 File 对象拿不到这个路径（浏览器安全限制），必须走仓库已有的"本地文件选择"
// 基础设施（features/workspace/localFilePicker.ts，ChatPanel 附件同一套）：
// - 点击选择：selectLocalFiles()——桌面端走 pywebview 原生选择框，浏览器端走后端
//   path.select_files 的原生选择框，两种情况下拿到的 LocalFilePick.path 都是真实绝对路径。
// - 拖拽：只有桌面壳（pywebview webview）才能拿到路径——操作系统级拖拽由 desktop_app.py 的
//   原生桥接经 registerDesktopLocalFilesConsumer 异步派发，携带真实 path；纯浏览器/whl 环境下
//   OS 文件拖拽天然拿不到路径，这里跟 ChatPanel/InputArea.tsx 的 handleFileDragOver 同一个限制
//   ——非桌面壳直接拒绝（dropEffect='none'），不做"能拖但拖了没用"的假交互。
// 上一版（同日更早）用浏览器 File 对象 + 假路径糊弄 UI，这版整个换成上面这套真实基础设施。
export function UploadFileCreateModal({ error, onCancel, onConfirm }: UploadFileCreateModalProps) {
  const { t } = useTranslation();
  const [filePick, setFilePick] = useState<LocalFilePick | null>(null);
  const [invalid, setInvalid] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [browsing, setBrowsing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const desktopReady = useDesktopLocalFilePickerReady();
  const dropAcceptUntilRef = useRef(0);
  const lastDropIdRef = useRef<string | null>(null);

  function acceptPick(pick: LocalFilePick | undefined) {
    if (!pick) return;
    if (!isAcceptedFilename(pick.filename)) {
      setInvalid(true);
      setFilePick(null);
      return;
    }
    setInvalid(false);
    setFilePick(pick);
  }

  async function handleBrowse() {
    if (browsing || filePick) return;
    setBrowsing(true);
    try {
      const result = await selectLocalFiles(false);
      if (result.ok) acceptPick(result.files[0]);
    } finally {
      setBrowsing(false);
    }
  }

  function handleRemove() {
    setFilePick(null);
    setInvalid(false);
  }

  // 桌面壳拖拽：弹窗打开期间接管 desktop-local-files 消费者。ConnectorMarket 面板和 ChatPanel
  // 是 activeNav 下互斥挂载的视图，弹窗关闭时 unregister 不会顶掉一个仍在运行的 ChatPanel
  // 消费者（同一时刻只有一边真的挂载着）。
  useEffect(() => {
    if (!desktopReady) return undefined;
    const unregister = registerDesktopLocalFilesConsumer((detail, files) => {
      if (detail?.source && detail.source !== 'drop') return;
      if (!files.length) return;
      const dropId = typeof detail?.dropId === 'string' ? detail.dropId : null;
      if (dropId && lastDropIdRef.current === dropId) return;
      const clientX = detail?.clientX;
      const clientY = detail?.clientY;
      const hasCoords = typeof clientX === 'number' && typeof clientY === 'number';
      let inZone = false;
      if (hasCoords) {
        const hit = document.elementFromPoint(clientX, clientY);
        inZone = Boolean(hit?.closest(`.${DROP_ZONE_CLASS}`));
      }
      const trusted = detail?.trusted === true;
      const acceptByTime = Date.now() <= dropAcceptUntilRef.current;
      if (!trusted && !acceptByTime && !inZone) return;
      if (dropId) lastDropIdRef.current = dropId;
      setDragActive(false);
      acceptPick(files[0]);
    });
    return unregister;
  }, [desktopReady]);

  useEffect(() => {
    const onFileDrag = (event: Event) => {
      const active = Boolean((event as CustomEvent<{ active?: boolean }>).detail?.active);
      if (active) dropAcceptUntilRef.current = Date.now() + DROP_ACCEPT_WINDOW_MS;
    };
    window.addEventListener(DESKTOP_FILE_DRAG_EVENT, onFileDrag as EventListener);
    return () => window.removeEventListener(DESKTOP_FILE_DRAG_EVENT, onFileDrag as EventListener);
  }, []);

  async function handleConfirm() {
    if (!filePick || submitting) return;
    setSubmitting(true);
    try {
      await onConfirm(filePick.path);
    } finally {
      setSubmitting(false);
    }
  }

  const displayError = error
    ? mapLocalPackageImportError(error, t, {
        readme: 'connectorMarket.upload.missingReadme',
        manifest: 'connectorMarket.upload.missingManifest',
      })
    : null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-overlay-cron-dialog" data-testid="connector-market-upload-modal">
      <div className="relative w-[520px] rounded-2xl bg-card p-6 shadow-xl">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-[16px] font-semibold leading-6 text-text">{t('connectorMarket.create.withUpload')}</h2>
          <button type="button" onClick={onCancel} className="text-text-muted hover:text-text" data-testid="connector-market-upload-modal-close">
            <X size={18} />
          </button>
        </div>

        <div className="mb-4 flex gap-2 rounded-lg bg-accent-subtle px-3 py-2.5 text-[12px] leading-[18px] text-text" data-testid="connector-market-upload-modal-hint">
          <Info size={14} className="mt-0.5 shrink-0 text-[color:var(--color-chat-accent)]" />
          <span>{t('connectorMarket.upload.hint')}</span>
        </div>

        <div
          onDragOver={(event) => {
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
          onDrop={(event) => {
            if (!Array.from(event.dataTransfer.types).includes('Files')) return;
            event.preventDefault();
            setDragActive(false);
            // 真正带 path 的文件经桌面桥接的 registerDesktopLocalFilesConsumer 异步到达，这里
            // 只负责吃掉浏览器原生 drop 事件，避免触发系统默认的"在新标签页打开文件"行为。
          }}
          onClick={handleBrowse}
          data-testid="connector-market-upload-dropzone"
          className={`${DROP_ZONE_CLASS} flex h-40 flex-col items-center justify-center gap-2 rounded-xl border border-dashed bg-bg text-text-muted transition-colors ${
            filePick ? 'cursor-default' : 'cursor-pointer'
          } ${
            invalid
              ? 'border-danger'
              : dragActive
                ? 'border-[color:var(--color-chat-accent)]'
                : 'border-border-strong hover:border-border-hover'
          }`}
        >
          {filePick ? (
            <div className="flex w-full items-center gap-2.5 px-5" data-testid="connector-market-upload-selected-file">
              <FileArchive size={22} className="shrink-0 text-text-muted" />
              <div className="min-w-0 flex-1">
                <p className="truncate text-[13px] text-text" title={filePick.filename}>{filePick.filename}</p>
                <p className="text-[11px] text-text-muted">{formatFileSize(filePick.size)}</p>
              </div>
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  handleRemove();
                }}
                className="shrink-0 text-text-muted hover:text-text"
                data-testid="connector-market-upload-remove-file"
              >
                <X size={16} />
              </button>
            </div>
          ) : browsing ? (
            <Loader2 size={22} className="animate-spin" data-testid="connector-market-upload-browsing" />
          ) : (
            <div className="flex h-full w-full flex-col items-center justify-center gap-2">
              <UploadCloud size={22} />
              <span className="text-[13px]">{t('connectorMarket.upload.dropHint')}</span>
            </div>
          )}
        </div>
        {invalid && <p className="mt-1.5 text-[11px] text-danger" data-testid="connector-market-upload-invalid">{t('connectorMarket.upload.invalidType')}</p>}
        {displayError ? (
          <p className="mt-1.5 text-[11px] text-danger" role="alert" data-testid="connector-market-upload-error">
            {displayError}
          </p>
        ) : null}

        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onCancel} className="rounded-lg border border-border px-4 py-1.5 text-[13px] text-text hover:border-border-hover" data-testid="connector-market-upload-cancel">
            {t('connectorMarket.common.cancel')}
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={!filePick || submitting}
            className="rounded-lg bg-text px-4 py-1.5 text-[13px] text-text-inverse disabled:opacity-60"
            data-testid="connector-market-upload-confirm"
          >
            {t('connectorMarket.common.confirm')}
          </button>
        </div>
      </div>
    </div>
  );
}
