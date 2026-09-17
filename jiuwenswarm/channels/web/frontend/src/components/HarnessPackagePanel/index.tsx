/**
 * HarnessPackagePanel Component
 *
 * Plugin version management panel for auto-harness.
 * Displays version selector, version info, file tree, and hot-activate button.
 */

import { useEffect, useState, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useHarnessStore } from '../../stores';
import { webRequest } from '../../services/webClient';
import { PackageInfo, PackagesPayload, ActivatePayload, DeactivatePayload } from '../../types';
import { HarnessExtensionTree } from '../ToolPanel/HarnessExtensionTree';
import { resolveHarnessError } from '../../utils';
import './HarnessPackagePanel.css';

interface HarnessPackagePanelProps {
  sessionId: string;
}

interface DeletePayload {
  deleted_package_id: string;
  extension_name: string;
  switched_to_native: boolean;
  message: string;
}

export function HarnessPackagePanel({ sessionId }: HarnessPackagePanelProps) {
  const { t } = useTranslation();
  const {
    packages,
    nativeVersion,
    activePackageIds,
    selectedPackageId,
    loadingPackages,
    activatingPackage,
    deactivatingPackage,
    isPackageActive,
    setPackages,
    setSelectedPackageId,
    setLoadingPackages,
    setActivatingPackage,
    setDeactivatingPackage,
  } = useHarnessStore();

  const [loadError, setLoadError] = useState<string | null>(null);
  const [activateError, setActivateError] = useState<string | null>(null);
  const [activateSuccess, setActivateSuccess] = useState<string | null>(null);
  const [deactivateError, setDeactivateError] = useState<string | null>(null);
  const [deactivateSuccess, setDeactivateSuccess] = useState<string | null>(null);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [deletingPackage, setDeletingPackage] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleteSuccess, setDeleteSuccess] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [importSuccess, setImportSuccess] = useState<string | null>(null);
  const [exportSuccess, setExportSuccess] = useState<string | null>(null);
  const [deactivatingAll, setDeactivatingAll] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Fetch packages from cache (initial load)
  const fetchPackages = useCallback(async () => {
    setLoadingPackages(true);
    setLoadError(null);

    try {
      const payload = await webRequest<PackagesPayload>('harness.packages', undefined);
      const activeIds = payload.active_package_ids || [];
      setPackages(
        payload.packages || [],
        payload.native_version || { id: 'native', extension_name: 'Native Agent', is_active: true },
        activeIds
      );
    } catch (err) {
      console.error('Failed to fetch packages:', err);
      setLoadError(err instanceof Error ? err.message : t('harnessPackage.loadPackagesFailed'));
    } finally {
      setLoadingPackages(false);
    }
  }, [setLoadingPackages, setPackages, t]);

  // Re-scan packages directory (refresh button)
  const scanPackages = useCallback(async () => {
    setLoadingPackages(true);
    setLoadError(null);

    try {
      const payload = await webRequest<PackagesPayload>('harness.packages.scan', undefined);
      const activeIds = payload.active_package_ids || [];
      setPackages(
        payload.packages || [],
        payload.native_version || { id: 'native', extension_name: 'Native Agent', is_active: true },
        activeIds
      );
    } catch (err) {
      console.error('Failed to scan packages:', err);
      setLoadError(err instanceof Error ? err.message : t('harnessPackage.loadPackagesFailed'));
    } finally {
      setLoadingPackages(false);
    }
  }, [setLoadingPackages, setPackages, t]);

  // Initial load - scan packages directory (refresh) on mount
  useEffect(() => {
    fetchPackages();
  }, [fetchPackages]);

  // Refresh packages when user switches back to this tab
  useEffect(() => {
    const handler = () => {
      if (!document.hidden) fetchPackages();
    };
    document.addEventListener('visibilitychange', handler);
    return () => document.removeEventListener('visibilitychange', handler);
  }, [fetchPackages]);

  // Get selected package info
  const getSelectedPackage = useCallback((): PackageInfo | null => {
    if (!selectedPackageId || selectedPackageId === 'native') return null;
    return packages.find(p => p.id === selectedPackageId) || null;
  }, [selectedPackageId, packages]);

  const selectedPackage = getSelectedPackage();

  // Check if selected is native version
  const isSelectedNative = selectedPackageId === 'native';

  // Check if selected package is active
  const isSelectedActive = selectedPackageId ? isPackageActive(selectedPackageId) : false;

  // Handle version selection change
  const handleVersionChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const value = e.target.value;
    setSelectedPackageId(value || 'native');
    // Clear status messages when switching
    setActivateError(null);
    setActivateSuccess(null);
    setDeactivateError(null);
    setDeactivateSuccess(null);
    setDeleteError(null);
    setDeleteSuccess(null);
    setImportError(null);
    setImportSuccess(null);
    setExportError(null);
    setExportSuccess(null);
  };

  // Handle activate/deactivate based on current status
  const handleToggleActive = async () => {
    if (!selectedPackageId || selectedPackageId === 'native') return;

    if (isSelectedActive) {
      // Deactivate
      setDeactivatingPackage(true);
      setDeactivateError(null);
      setDeactivateSuccess(null);

      try {
        await webRequest<DeactivatePayload>('harness.deactivate', {
          package_id: selectedPackageId,
        });

        setDeactivateSuccess(t('harnessPackage.deactivateSuccess'));
        await fetchPackages();
      } catch (err) {
        console.error('Failed to deactivate package:', err);
        setDeactivateError(resolveHarnessError(err, 'harnessPackage.deactivateFailed'));
      } finally {
        setDeactivatingPackage(false);
      }
    } else {
      // Activate
      setActivatingPackage(true);
      setActivateError(null);
      setActivateSuccess(null);

      try {
        const payload = await webRequest<ActivatePayload>('harness.activate', {
          package_id: selectedPackageId,
          session_id: sessionId,
        });

        setActivateSuccess(t('harnessPackage.activateSuccess'));

        // Update extensionReady for the activated package
        useHarnessStore.getState().setExtensionReady(sessionId, {
          extensionName: payload.extension_name,
          runtimePath: payload.runtime_path,
          configPath: payload.config_path,
          verifyReport: {},
          componentsSummary: {},
        });

        await fetchPackages();
      } catch (err) {
        console.error('Failed to activate package:', err);
        setActivateError(resolveHarnessError(err, 'harnessPackage.activateFailed'));
      } finally {
        setActivatingPackage(false);
      }
    }
  };

  // Handle deactivate all packages
  const handleDeactivateAll = async () => {
    if (activePackageIds.length === 0) return;

    setDeactivatingAll(true);
    setDeactivateError(null);
    setDeactivateSuccess(null);

    try {
      // Deactivate each active package
      for (const packageId of activePackageIds) {
        await webRequest<DeactivatePayload>('harness.deactivate', {
          package_id: packageId,
        });
      }

      setDeactivateSuccess(t('harnessPackage.allDeactivated'));
      useHarnessStore.getState().setExtensionReady(sessionId, null);
      await fetchPackages();
    } catch (err) {
      console.error('Failed to deactivate all packages:', err);
      setDeactivateError(resolveHarnessError(err, 'harnessPackage.deactivateFailed'));
    } finally {
      setDeactivatingAll(false);
    }
  };

  // Handle delete button click - open confirmation modal
  const handleDeleteClick = () => {
    if (!selectedPackage || isSelectedNative) return;
    setDeleteConfirmOpen(true);
    setDeleteError(null);
    setDeleteSuccess(null);
  };

  // Handle delete confirmation
  const handleDeleteConfirm = async () => {
    if (!selectedPackage) return;

    setDeletingPackage(true);
    setDeleteError(null);
    setDeleteSuccess(null);

    try {
      const payload = await webRequest<DeletePayload>('harness.delete', {
        package_id: selectedPackageId,
      });

      // If switched to native, update state
      if (payload.switched_to_native) {
        useHarnessStore.getState().setExtensionReady(sessionId, null);
        setSelectedPackageId('native');
      }

      // Refresh packages list
      await fetchPackages();

      setDeleteSuccess(t('harnessPackage.deleteSuccess'));
      setDeleteConfirmOpen(false);
    } catch (err) {
      console.error('Failed to delete package:', err);
      setDeleteError(resolveHarnessError(err, 'harnessPackage.deleteFailed'));
    } finally {
      setDeletingPackage(false);
    }
  };

  // Handle delete modal close
  const handleDeleteCancel = () => {
    setDeleteConfirmOpen(false);
  };

  // Handle export - download package as zip via WebSocket
  const handleExport = async () => {
    if (!selectedPackageId || isSelectedNative) return;

    setExporting(true);
    setExportError(null);
    setExportSuccess(null);

    try {
      // Send via WebSocket - now returns download URL instead of base64 content
      const result = await webRequest<{
        download_url?: string;  // new format - HTTP download URL
        file_content?: string;  // legacy format - base64 encoded
        filename: string;
      }>('harness.export', {
        package_id: selectedPackageId,
      });

      if (result.download_url) {
        // New format: direct HTTP download (avoids WebSocket size limits)
        const a = document.createElement('a');
        a.href = result.download_url;
        a.download = result.filename || `${selectedPackage?.extension_name || 'package'}.zip`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      } else if (result.file_content) {
        // Legacy format: decode base64 and download (for backwards compatibility)
        const binaryString = atob(result.file_content);
        const bytes = new Uint8Array(binaryString.length);
        for (let i = 0; i < binaryString.length; i++) {
          bytes[i] = binaryString.charCodeAt(i);
        }
        const blob = new Blob([bytes], { type: 'application/zip' });
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = result.filename || `${selectedPackage?.extension_name || 'package'}.zip`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        window.URL.revokeObjectURL(url);
      }

      setExportSuccess(t('harnessPackage.exportSuccess'));
    } catch (err) {
      console.error('Export failed:', err);
      setExportError(resolveHarnessError(err, 'harnessPackage.exportError'));
    } finally {
      setExporting(false);
    }
  };

  // Handle import button click - open file picker
  const handleImportClick = () => {
    fileInputRef.current?.click();
  };

  // Handle file selection for import
  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setImporting(true);
    setImportError(null);
    setImportSuccess(null);

    try {
      // Read file and convert to base64
      const fileContent = await file.arrayBuffer();
      const base64Content = btoa(
        new Uint8Array(fileContent).reduce(
          (data, byte) => data + String.fromCharCode(byte),
          ''
        )
      );

      // Send via WebSocket
      await webRequest('harness.import', {
        file_content: base64Content,
      });

      setImportSuccess(t('harnessPackage.importSuccess'));
      // Refresh package list
      await fetchPackages();
    } catch (err) {
      console.error('Import failed:', err);
      setImportError(resolveHarnessError(err, 'harnessPackage.importError'));
    } finally {
      setImporting(false);
      // Reset file input
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    }
  };

  // Build version options for dropdown
  const buildVersionOptions = () => {
    const options: JSX.Element[] = [];

    // Native version option (always first)
    options.push(
      <option key="native" value="native">
        {nativeVersion?.extension_name || 'Native Agent'}
        {nativeVersion?.is_active ? ` (${t('harnessPackage.currentActive')})` : ''}
      </option>
    );

    // Package options
    packages.forEach((pkg) => {
      const isActive = isPackageActive(pkg.id);
      options.push(
        <option key={pkg.id} value={pkg.id}>
          {pkg.extension_name}
          {pkg.version_label ? ` - ${pkg.version_label}` : ''}
          {isActive ? ` (${t('harnessPackage.currentActive')})` : ''}
        </option>
      );
    });

    return options;
  };

  // Format date for display
  const formatDate = (dateStr: string | undefined) => {
    if (!dateStr) return '-';
    try {
      const date = new Date(dateStr);
      return date.toLocaleString();
    } catch {
      return dateStr;
    }
  };

  // Render version info
  const renderVersionInfo = () => {
    if (isSelectedNative) {
      return (
        <div className="harness-package-panel__info" data-testid="harness-package-panel-info" data-variant="native">
          <div className="harness-package-panel__info-title" data-testid="harness-package-panel-info-title" data-variant="native">
            {t('harnessPackage.versionInfo')}
          </div>
          <div className="harness-package-panel__info-item" data-testid="harness-package-panel-info-item" data-variant="native_version_type">
            <span className="harness-package-panel__info-label" data-testid="harness-package-panel-info-label" data-variant="native_version_type">
              {t('harnessPackage.versionType')}
            </span>
            <span className="harness-package-panel__info-badge" data-testid="harness-package-panel-info-badge" data-variant="native_version_type">
              {t('harnessPackage.nativeVersion')}
            </span>
          </div>
          <div className="harness-package-panel__info-item" data-testid="harness-package-panel-info-item" data-variant="native_status">
            <span className="harness-package-panel__info-label" data-testid="harness-package-panel-info-label" data-variant="native_status">
              {t('harnessPackage.status')}
            </span>
            <span className={`harness-package-panel__info-badge ${nativeVersion?.is_active ? 'harness-package-panel__info-badge--active' : ''}`} data-testid="harness-package-panel-info-badge" data-variant="native_status">
              {nativeVersion?.is_active ? t('harnessPackage.active') : t('harnessPackage.inactive')}
            </span>
          </div>
          <div className="harness-package-panel__info-item" data-testid="harness-package-panel-info-item" data-variant="native_description">
            <span className="harness-package-panel__info-label" data-testid="harness-package-panel-info-label" data-variant="native_description">
              {t('harnessPackage.description')}
            </span>
            <span className="harness-package-panel__info-value" data-testid="harness-package-panel-info-value" data-variant="native_description">
              {t('harnessPackage.nativeDescription')}
            </span>
          </div>
        </div>
      );
    }

    if (!selectedPackage) {
      return null;
    }

    const isActive = isPackageActive(selectedPackage.id);

    return (
      <div className="harness-package-panel__info" data-testid="harness-package-panel-info" data-variant="package">
        <div className="harness-package-panel__info-title" data-testid="harness-package-panel-info-title" data-variant="package">
          {t('harnessPackage.versionInfo')}
        </div>
        <div className="harness-package-panel__info-item" data-testid="harness-package-panel-info-item" data-variant="package_extension_name">
          <span className="harness-package-panel__info-label" data-testid="harness-package-panel-info-label" data-variant="package_extension_name">
            {t('harnessPackage.extensionName')}
          </span>
          <span className="harness-package-panel__info-value" data-testid="harness-package-panel-info-value" data-variant="package_extension_name">
            {selectedPackage.extension_name}
          </span>
        </div>
        <div className="harness-package-panel__info-item" data-testid="harness-package-panel-info-item" data-variant="package_version_type">
          <span className="harness-package-panel__info-label" data-testid="harness-package-panel-info-label" data-variant="package_version_type">
            {t('harnessPackage.versionType')}
          </span>
          <span className="harness-package-panel__info-badge" data-testid="harness-package-panel-info-badge" data-variant="package_version_type">
            {t('harnessPackage.harnessVersion')}
          </span>
        </div>
        <div className="harness-package-panel__info-item" data-testid="harness-package-panel-info-item" data-variant="package_status">
          <span className="harness-package-panel__info-label" data-testid="harness-package-panel-info-label" data-variant="package_status">
            {t('harnessPackage.status')}
          </span>
          <span className={`harness-package-panel__info-badge ${isActive ? 'harness-package-panel__info-badge--active' : ''}`} data-testid="harness-package-panel-info-badge" data-variant="package_status">
            {isActive ? t('harnessPackage.active') : t('harnessPackage.inactive')}
          </span>
        </div>
        <div className="harness-package-panel__info-item" data-testid="harness-package-panel-info-item" data-variant="package_created_at">
          <span className="harness-package-panel__info-label" data-testid="harness-package-panel-info-label" data-variant="package_created_at">
            {t('harnessPackage.createdAt')}
          </span>
          <span className="harness-package-panel__info-value" data-testid="harness-package-panel-info-value" data-variant="package_created_at">
            {formatDate(selectedPackage.created_at)}
          </span>
        </div>
        {isActive && selectedPackage.activated_at && (
          <div className="harness-package-panel__info-item" data-testid="harness-package-panel-info-item" data-variant="package_activated_at">
            <span className="harness-package-panel__info-label" data-testid="harness-package-panel-info-label" data-variant="package_activated_at">
              {t('harnessPackage.activatedAt')}
            </span>
            <span className="harness-package-panel__info-value" data-testid="harness-package-panel-info-value" data-variant="package_activated_at">
              {formatDate(selectedPackage.activated_at)}
            </span>
          </div>
        )}
        {selectedPackage.version_label && (
          <div className="harness-package-panel__info-item" data-testid="harness-package-panel-info-item" data-variant="package_version_label">
            <span className="harness-package-panel__info-label" data-testid="harness-package-panel-info-label" data-variant="package_version_label">
              {t('harnessPackage.versionLabel')}
            </span>
            <span className="harness-package-panel__info-value" data-testid="harness-package-panel-info-value" data-variant="package_version_label">
              {selectedPackage.version_label}
            </span>
          </div>
        )}
        <div className="harness-package-panel__info-item" data-testid="harness-package-panel-info-item" data-variant="package_runtime_path">
          <span className="harness-package-panel__info-label" data-testid="harness-package-panel-info-label" data-variant="package_runtime_path">
            {t('harnessPackage.runtimePath')}
          </span>
          <span className="harness-package-panel__info-value harness-package-panel__info-value--mono" data-testid="harness-package-panel-info-value" data-variant="package_runtime_path">
            {selectedPackage.runtime_path}
          </span>
        </div>
        {selectedPackage.description && (
          <div className="harness-package-panel__info-item" data-testid="harness-package-panel-info-item" data-variant="package_description">
            <span className="harness-package-panel__info-label" data-testid="harness-package-panel-info-label" data-variant="package_description">
              {t('harnessPackage.description')}
            </span>
            <span className="harness-package-panel__info-value" data-testid="harness-package-panel-info-value" data-variant="package_description">
              {selectedPackage.description}
            </span>
          </div>
        )}
      </div>
    );
  };

  if (loadingPackages) {
    return (
      <div className="harness-package-panel" data-testid="harness-package-panel-loading">
        <div className="harness-package-panel__loading" data-testid="harness-package-panel-loading-body">
          <div className="harness-package-panel__spinner" />
          <span data-testid="harness-package-panel-loading-text">{t('harnessPackage.loading')}</span>
        </div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="harness-package-panel" data-testid="harness-package-panel-error">
        <div className="harness-package-panel__error" data-testid="harness-package-panel-error-body">
          <span data-testid="harness-package-panel-error-text">{loadError}</span>
          <button
            type="button"
            onClick={fetchPackages}
            className="harness-package-panel__retry-btn"
            data-testid="harness-package-panel-error-retry-btn"
          >
            {t('harnessPackage.retry')}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="harness-package-panel" data-testid="harness-package-panel">
      {/* Header */}
      <div className="harness-package-panel__header" data-testid="harness-package-panel-header">
        <h3 data-testid="harness-package-panel-title">{t('harnessPackage.title')}</h3>
        <div className="harness-package-panel__header-actions" data-testid="harness-package-panel-header-actions">
          <button
            type="button"
            onClick={scanPackages}
            className="harness-package-panel__refresh-btn"
            disabled={loadingPackages}
            data-testid="harness-package-panel-refresh-btn"
          >
            {loadingPackages ? t('common.refreshing') : t('harnessPackage.refresh')}
          </button>
          {activePackageIds.length > 0 && (
            <button
              type="button"
              onClick={handleDeactivateAll}
              className="harness-package-panel__deactivate-all-btn"
              disabled={deactivatingAll}
              data-testid="harness-package-panel-deactivate-all-btn"
            >
              {deactivatingAll ? t('harnessPackage.deactivating') : t('harnessPackage.deactivateAll')}
            </button>
          )}
          <button
            type="button"
            onClick={handleImportClick}
            className="harness-package-panel__import-btn"
            disabled={importing}
            data-testid="harness-package-panel-import-btn"
          >
            {importing ? t('harnessPackage.importing') : t('harnessPackage.import')}
          </button>
          <button
            type="button"
            onClick={handleExport}
            className="harness-package-panel__export-btn"
            disabled={isSelectedNative || exporting || !selectedPackage}
            data-testid="harness-package-panel-export-btn"
          >
            {exporting ? t('harnessPackage.exporting') : t('harnessPackage.export')}
          </button>
        </div>
        {/* Hidden file input for import */}
        <input
          type="file"
          accept=".zip"
          ref={fileInputRef}
          onChange={handleFileSelect}
          style={{ display: 'none' }}
          data-testid="harness-package-panel-import-file-input"
        />
      </div>

      {/* Main content - Left: selector & info, Right: file tree */}
      <div className="harness-package-panel__main" data-testid="harness-package-panel-main">
        {/* Left side */}
        <div className="harness-package-panel__left" data-testid="harness-package-panel-left">
          {/* Version Selector */}
          <div className="harness-package-panel__selector" data-testid="harness-package-panel-selector">
            <label htmlFor="version-select" data-testid="harness-package-panel-selector-label">{t('harnessPackage.versionSelector')}</label>
            <select
              id="version-select"
              value={selectedPackageId || 'native'}
              onChange={handleVersionChange}
              className="harness-package-panel__select"
              data-testid="harness-package-panel-select"
            >
              {buildVersionOptions()}
            </select>
          </div>

          {/* Version Info */}
          {renderVersionInfo()}
        </div>

        {/* Right side - File Tree */}
        <div className="harness-package-panel__right" data-testid="harness-package-panel-right">
          <div className="harness-package-panel__tree" data-testid="harness-package-panel-tree">
            {selectedPackage ? (
              <HarnessExtensionTree
                key={selectedPackage.id}
                runtimePath={selectedPackage.runtime_path}
                extensionName={selectedPackage.extension_name}
                showExport={false}
              />
            ) : (
              <div className="harness-package-panel__no-extension" data-testid="harness-package-panel-no-extension">
                {t('harnessPackage.selectToViewFiles')}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Footer - Activate/Deactivate Section */}
      <div className="harness-package-panel__footer" data-testid="harness-package-panel-footer">
        <div className="harness-package-panel__actions" data-testid="harness-package-panel-actions">
          {/* Toggle Active Button - changes based on status */}
          {!isSelectedNative && (
            <button
              type="button"
              onClick={handleToggleActive}
              className={isSelectedActive ? 'harness-package-panel__deactivate-btn' : 'harness-package-panel__activate-btn'}
              disabled={activatingPackage || deactivatingPackage}
              data-testid="harness-package-panel-toggle-active-btn"
              data-variant={isSelectedActive ? 'deactivate' : 'activate'}
            >
              {activatingPackage
                ? t('harnessPackage.activating')
                : deactivatingPackage
                  ? t('harnessPackage.deactivating')
                  : isSelectedActive
                    ? t('harnessPackage.deactivate')
                    : t('harnessPackage.hotActivate')}
            </button>
          )}

          {/* Delete Button */}
          <button
            type="button"
            onClick={handleDeleteClick}
            className="harness-package-panel__delete-btn"
            disabled={isSelectedNative || deletingPackage || !selectedPackage}
            data-testid="harness-package-panel-delete-btn"
          >
            {deletingPackage
              ? t('harnessPackage.deleting')
              : t('harnessPackage.delete')}
          </button>
        </div>

        {/* Status Messages */}
        <div className="harness-package-panel__status" data-testid="harness-package-panel-status">
          {activateSuccess && (
            <div className="harness-package-panel__success" data-testid="harness-package-panel-success-message" data-variant="activate_result">
              {activateSuccess}
            </div>
          )}
          {activateError && (
            <div className="harness-package-panel__error-msg" data-testid="harness-package-panel-error-message" data-variant="activate_result">
              {activateError}
            </div>
          )}
          {deactivateSuccess && (
            <div className="harness-package-panel__success" data-testid="harness-package-panel-success-message" data-variant="deactivate_result">
              {deactivateSuccess}
            </div>
          )}
          {deactivateError && (
            <div className="harness-package-panel__error-msg" data-testid="harness-package-panel-error-message" data-variant="deactivate_result">
              {deactivateError}
            </div>
          )}
          {deleteSuccess && (
            <div className="harness-package-panel__success" data-testid="harness-package-panel-success-message" data-variant="delete_result">
              {deleteSuccess}
            </div>
          )}
          {deleteError && (
            <div className="harness-package-panel__error-msg" data-testid="harness-package-panel-error-message" data-variant="delete_result">
              {deleteError}
            </div>
          )}
          {importSuccess && (
            <div className="harness-package-panel__success" data-testid="harness-package-panel-success-message" data-variant="import_result">
              {importSuccess}
            </div>
          )}
          {importError && (
            <div className="harness-package-panel__error-msg" data-testid="harness-package-panel-error-message" data-variant="import_result">
              {importError}
            </div>
          )}
          {exportSuccess && (
            <div className="harness-package-panel__success" data-testid="harness-package-panel-success-message" data-variant="export_result">
              {exportSuccess}
            </div>
          )}
          {exportError && (
            <div className="harness-package-panel__error-msg" data-testid="harness-package-panel-error-message" data-variant="export_result">
              {exportError}
            </div>
          )}
        </div>
      </div>

      {/* Delete Confirmation Modal */}
      {deleteConfirmOpen && selectedPackage && (
        <div className="harness-package-panel__modal-overlay" onClick={handleDeleteCancel} data-testid="harness-package-panel-delete-confirm-overlay">
          <div className="harness-package-panel__modal" onClick={e => e.stopPropagation()} data-testid="harness-package-panel-delete-confirm-panel">
            <div className="harness-package-panel__modal-header" data-testid="harness-package-panel-delete-confirm-header">
              <h4 data-testid="harness-package-panel-delete-confirm-title">{t('harnessPackage.deleteConfirmTitle')}</h4>
            </div>
            <div className="harness-package-panel__modal-body" data-testid="harness-package-panel-delete-confirm-body">
              <p data-testid="harness-package-panel-delete-confirm-message">
                {t('harnessPackage.deleteConfirmMessage', {
                  extensionName: selectedPackage.extension_name,
                })}
              </p>
              {isPackageActive(selectedPackage.id) && (
                <p className="harness-package-panel__modal-warning" data-testid="harness-package-panel-delete-confirm-warning">
                  {t('harnessPackage.deleteActiveWarning')}
                </p>
              )}
            </div>
            <div className="harness-package-panel__modal-footer" data-testid="harness-package-panel-delete-confirm-footer">
              <button
                type="button"
                onClick={handleDeleteCancel}
                className="harness-package-panel__modal-cancel-btn"
                disabled={deletingPackage}
                data-testid="harness-package-panel-delete-confirm-cancel-btn"
              >
                {t('common.cancel')}
              </button>
              <button
                type="button"
                onClick={handleDeleteConfirm}
                className="harness-package-panel__modal-confirm-btn"
                disabled={deletingPackage}
                data-testid="harness-package-panel-delete-confirm-btn"
              >
                {deletingPackage ? t('harnessPackage.deleting') : t('common.confirm')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}