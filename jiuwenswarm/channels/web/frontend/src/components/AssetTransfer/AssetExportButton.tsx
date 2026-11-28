// 通用资产导出按钮：assets.export 取短效下载链接 → fetch 下载 →
// desktopSave 双路径保存。保存结果四分支（桌面已保存 / 浏览器已保存 /
// 浏览器开始下载 / 用户取消）与八类 ASSET_* 契约错误在本组件内统一转
// Toast；挂载方只提供 kind/localId，各 kind 的差异全部在服务端适配器层。
import { useCallback, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Download } from 'lucide-react';
import {
  assetIssueKey,
  exportAssetToLocalFile,
  type AssetKind,
  type AssetTransferDeps,
} from '../../features/assetTransfer';
import type { BlobSaveResult } from '../../utils/desktopSave';
import { toast } from '../ui/Toast/toastStore';

export type AssetExportButtonProps = {
  kind: AssetKind;
  /** 资产本地 id；口径与发布一致（专家传 runtimePackageName，专家团/技能传名称）。 */
  localId: string;
  /** 缺省导出当前版本。 */
  version?: string;
  /** 详情页缺省文字链接态 detail-action；列表卡等其他挂载点可覆盖。 */
  className?: string;
  testId?: string;
  icon?: ReactNode;
  /** 挂载页统一 busy（与内部导出中状态合并禁用）。 */
  disabled?: boolean;
  /** 传输依赖注入（测试用）；缺省走 webRequest / fetch / desktopSave。 */
  deps?: AssetTransferDeps;
  /** 保存完成回调（含用户取消与保存失败），供挂载方联动刷新。 */
  onResult?: (result: BlobSaveResult) => void;
};

// 同一 id 原地更新：连续导出时结果 Toast 不堆叠。
const TOAST_ID = 'asset-transfer-export';

function saveMessageKey(result: BlobSaveResult): string {
  if (result.outcome === 'failed') return 'assetTransfer.errors.saveFailed';
  if (result.outcome === 'cancelled') return 'assetTransfer.messages.exportCancelled';
  if (result.transport === 'desktop') return 'assetTransfer.messages.exportSavedDesktop';
  if (result.transport === 'browser-file-picker') return 'assetTransfer.messages.exportSavedBrowser';
  return 'assetTransfer.messages.exportStartedBrowser';
}

export function AssetExportButton({
  kind,
  localId,
  version,
  className = 'detail-action',
  testId,
  icon,
  disabled = false,
  deps,
  onResult,
}: AssetExportButtonProps) {
  const { t } = useTranslation();
  const [exporting, setExporting] = useState(false);
  const exportingRef = useRef(false);

  const handleExport = useCallback(async () => {
    if (exportingRef.current) return;
    exportingRef.current = true;
    setExporting(true);
    try {
      const result = await exportAssetToLocalFile({ kind, local_id: localId, version }, deps);
      toast.open({
        id: TOAST_ID,
        content: t(saveMessageKey(result)),
        variant: result.outcome === 'saved' ? 'success' : result.outcome === 'cancelled' ? 'info' : 'error',
      });
      onResult?.(result);
    } catch (error) {
      toast.open({ id: TOAST_ID, content: t(assetIssueKey(error)), variant: 'error' });
    } finally {
      exportingRef.current = false;
      setExporting(false);
    }
  }, [deps, kind, localId, onResult, t, version]);

  const busy = disabled || exporting;
  return (
    <button
      type="button"
      className={className}
      data-testid={testId}
      disabled={busy}
      aria-busy={busy}
      onClick={() => void handleExport()}
    >
      {icon ?? <Download size={16} aria-hidden="true" />}
      {exporting ? t('assetTransfer.actions.exporting') : t('assetTransfer.actions.export')}
    </button>
  );
}
