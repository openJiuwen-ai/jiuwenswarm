import { useState } from 'react';
import { Pencil } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useChatStore } from '../../stores';
import { useSessionStore } from '../../stores/sessionStore';
import { FileIcon } from '../../components/FileIcon';
import type { ArtifactItem } from '../../components/ArtifactsPanel/artifactCollection';
import { registerPreviewableArtifact } from '../../components/ArtifactsPanel/artifactPreviewRegistry';
import { getFileIdentityKey } from '../../utils/fileDownloadDedup';
import { openArtifactPanel } from '../teamPanelState';
import { openSingleAgentPanel } from '../singleAgentPanelState';
import { selectSessionAssets, useSessionAssetsStore, type SessionAsset } from './sessionAssets';

const IMAGE_MIME_BY_EXT: Record<string, string> = {
  apng: 'image/apng',
  avif: 'image/avif',
  bmp: 'image/bmp',
  gif: 'image/gif',
  jpeg: 'image/jpeg',
  jpg: 'image/jpeg',
  png: 'image/png',
  svg: 'image/svg+xml',
  webp: 'image/webp',
};
const VIDEO_MIME_BY_EXT: Record<string, string> = {
  mkv: 'video/x-matroska',
  mov: 'video/quicktime',
  mp4: 'video/mp4',
  webm: 'video/webm',
};

/** 预览面板是按 MIME 判断怎么渲染的；素材名可能没有扩展名（如 serum），所以从 path 上取。 */
function assetMimeType(asset: SessionAsset): string {
  const ext = asset.path.split('.').pop()?.toLowerCase() ?? '';
  if (asset.kind === 'image') return IMAGE_MIME_BY_EXT[ext] ?? 'image/png';
  if (asset.kind === 'video') return VIDEO_MIME_BY_EXT[ext] ?? 'video/mp4';
  if (asset.kind === 'audio') return 'audio/mpeg';
  return 'application/octet-stream';
}

/** 当前任务的素材列表：显示名称（聊天框里 @名称 引用它），可以重命名。 */
export function SessionAssetList({ hideTitle = false }: { hideTitle?: boolean }) {
  const { t } = useTranslation();
  const sessionId = useChatStore((s) => s.activeSessionId);
  const assets = useSessionAssetsStore(selectSessionAssets(sessionId));
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState('');

  if (!sessionId) return null;

  const startEdit = (asset: SessionAsset) => {
    setEditingId(asset.asset_id);
    setDraft(asset.name);
    setError('');
  };
  const commit = async (asset: SessionAsset) => {
    const next = draft.trim();
    if (next === asset.name) {
      setEditingId(null);
      return;
    }
    try {
      await useSessionAssetsStore.getState().rename(sessionId, asset.asset_id, next);
      setEditingId(null);
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  // 和「产物」一样：点一下就打开右侧预览。素材不一定在消息产物里（只有走过
  // send_file_to_user 的才会进 buildArtifacts），所以先把它换算成产物形态登记进 registry，
  // 产物面板查不到列表就能查到它，预览就能打开。
  const openAsset = (asset: SessionAsset) => {
    const mimeType = assetMimeType(asset);
    const item: ArtifactItem = {
      id: `file:${getFileIdentityKey({ name: asset.name, path: asset.path })}`,
      name: asset.name,
      mimeType,
      path: asset.path,
      source: 'message',
    };
    registerPreviewableArtifact(item);
    const mode = useSessionStore.getState().runtimes[sessionId]?.mode ?? 'agent';
    if (mode === 'team' || mode === 'auto_harness') {
      openArtifactPanel(item.id);
      return;
    }
    openSingleAgentPanel('artifacts', item.id);
  };

  return (
    <section className="mb-4" data-testid="session-asset-section">
      {hideTitle ? null : <h3 className="mb-2 text-xs font-semibold text-text-muted">{t('sessionAssets.title')}</h3>}
      {assets.length === 0 ? (
        <p className="text-xs text-text-muted" data-testid="session-asset-empty">
          {t('sessionAssets.empty')}
        </p>
      ) : (
        <ul className="space-y-1" data-testid="session-asset-list">
          {assets.map((asset) => (
            <li
              key={asset.asset_id}
              className="group flex h-9 cursor-pointer items-center gap-2 rounded-md px-2 text-sm text-text hover:bg-[var(--color-tool-tab-active-bg)]"
              data-testid="session-asset-item"
              data-asset-name={asset.name}
              role="button"
              tabIndex={0}
              title={asset.path}
              onClick={() => {
                if (editingId === asset.asset_id) return;
                openAsset(asset);
              }}
              onKeyDown={(e) => {
                if (editingId === asset.asset_id) return;
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  openAsset(asset);
                }
              }}
            >
              <FileIcon fileName={asset.path} size={16} className="shrink-0" />
              {editingId === asset.asset_id ? (
                <input
                  autoFocus
                  value={draft}
                  maxLength={60}
                  className="min-w-0 flex-1 rounded border border-border bg-transparent px-1 text-sm"
                  data-testid="session-asset-rename-input"
                  aria-label={t('sessionAssets.rename')}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') void commit(asset);
                    if (e.key === 'Escape') {
                      setEditingId(null);
                      setError('');
                    }
                  }}
                  onBlur={() => void commit(asset)}
                />
              ) : (
                <span className="min-w-0 flex-1 truncate" data-testid="session-asset-name" title={asset.path}>
                  @{asset.name}
                </span>
              )}
              <button
                type="button"
                className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-text-muted opacity-0 group-hover:opacity-100 hover:bg-secondary hover:text-text"
                title={t('sessionAssets.rename')}
                aria-label={t('sessionAssets.rename')}
                data-testid="session-asset-rename"
                onMouseDown={(e) => e.preventDefault()}
                onClick={(e) => {
                  e.stopPropagation();
                  startEdit(asset);
                }}
              >
                <Pencil size={14} />
              </button>
            </li>
          ))}
        </ul>
      )}
      {error ? (
        <p className="mt-1 text-xs text-red-500" role="alert" data-testid="session-asset-error">
          {error}
        </p>
      ) : null}
    </section>
  );
}
