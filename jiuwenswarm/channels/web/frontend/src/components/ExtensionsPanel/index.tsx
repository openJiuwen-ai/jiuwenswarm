import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { webRequest } from '../../services/webClient';
import './ExtensionsPanel.css';

interface RailExtension {
  name: string;
  class_name: string;
  enabled: boolean;
  description: string;
  priority: number;
}

// 后端 extensions.import 校验失败时直接抛出中文异常原文（未走 i18n，见
// rail_manager.py:import_extension / agent_ws_server.py:_handle_extensions_import）。
// 这里按已知文案模式做前端翻译兜底；匹配不到的未知错误原样展示（可能仍是中文原文）。
function translateImportError(rawMessage: string, t: TFunction): string {
  const notFound = rawMessage.match(/^文件夹不存在或不是目录: (.+)$/);
  if (notFound) return t('extensions.errors.folderNotFound', { path: notFound[1] });

  const invalidName = rawMessage.match(/^文件夹名称 '(.+)' 必须是有效的英文标识符$/);
  if (invalidName) return t('extensions.errors.invalidFolderName', { name: invalidName[1] });

  const alreadyExists = rawMessage.match(/^扩展 '(.+)' 已存在$/);
  if (alreadyExists) return t('extensions.errors.alreadyExists', { name: alreadyExists[1] });

  if (rawMessage === '缺少 folder_path 参数') return t('extensions.errors.missingFolderPath');
  if (rawMessage === '扩展文件夹必须包含 rail.py 文件') return t('extensions.errors.missingRailFile');
  if (rawMessage === 'rail.py 验证失败') return t('extensions.errors.railValidationFailed');

  return rawMessage;
}

interface ExtensionsPanelProps {
  isConnected: boolean;
}

export function ExtensionsPanel({ isConnected }: ExtensionsPanelProps) {
  const { t } = useTranslation();
  const [extensions, setExtensions] = useState<RailExtension[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [folderPath, setFolderPath] = useState('');

  const loadExtensions = useCallback(async () => {
    if (!isConnected) return;

    setLoading(true);
    setError(null);

    try {
      const payload = await webRequest<{ extensions: RailExtension[] }>(
        'extensions.list',
        {}
      );

      if (payload?.extensions) {
        setExtensions(payload.extensions);
      } else {
        throw new Error(t('extensions.loadListFailed'));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [isConnected]);

  useEffect(() => {
    loadExtensions();
  }, [loadExtensions]);

  const handleImport = useCallback(async () => {
    if (!folderPath.trim()) {
      setError(t('extensions.enterFolderPath'));
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const extension = await webRequest<RailExtension>('extensions.import', {
        folder_path: folderPath,
      });

      if (extension) {
        setExtensions((prev) => [...prev, extension]);
        setFolderPath(''); // 清空输入框
      } else {
        throw new Error(t('extensions.importFailed'));
      }
    } catch (err) {
      const rawMessage = err instanceof Error ? err.message : String(err);
      setError(translateImportError(rawMessage, t));
    } finally {
      setLoading(false);
    }
  }, [folderPath, t]);

  const handleDelete = useCallback(
    async (name: string) => {
      if (!confirm(t('extensions.confirmDelete', { name }))) {
        return;
      }

      setLoading(true);
      setError(null);

      try {
        await webRequest('extensions.delete', { name });
        setExtensions((prev) => prev.filter((ext) => ext.name !== name));
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setLoading(false);
      }
    },
    []
  );

  const handleToggle = useCallback(
    async (name: string, enabled: boolean) => {
      // 先在本地临时更新 UI，提升响应速度
      const previousState = extensions.find(ext => ext.name === name);
      if (!previousState) return;

      // 立即更新 UI 状态
      setExtensions(prev =>
        prev.map(ext => (ext.name === name ? { ...ext, enabled } : ext))
      );

      setLoading(true);
      setError(null);

      try {
        const extension = await webRequest<RailExtension>('extensions.toggle', { name, enabled });
        console.log("===> extension:", extension)
        if (extension) {
          setExtensions(prev =>
            prev.map(ext => (ext.name === name ? extension : ext))
          );
        } else {
          throw new Error(t('extensions.toggleFailed'));
        }
      } catch (err) {
        // 发生错误，回滚到之前的状态
        setError(err instanceof Error ? err.message : String(err));
        setExtensions(prev =>
          prev.map(ext => (ext.name === name ? previousState : ext))
        );
      } finally {
        setLoading(false);
      }
    },
    [extensions]
  );

  return (
    <div className="extensions-panel">
      <div className="extensions-panel__title">
        {t('extensions.title')}
      </div>
      <p className="extensions-panel__description">
        {t('extensions.description')}
      </p>

      {error && (
        <div className="extensions-panel__error">
          {error}
        </div>
      )}

      {/* 导入区域 */}
      <div className="extensions-panel__import-section">
        <h3 className="extensions-panel__import-title">
          {t('extensions.importTitle')}
        </h3>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
          <div style={{ display: 'flex', gap: '0.5rem' }}>
            <input
              type="text"
              value={folderPath}
              onChange={(e) => setFolderPath(e.target.value)}
              placeholder={t('extensions.folderPathPlaceholder')}
              disabled={loading || !isConnected}
              style={{
                flex: 1,
                padding: '0.5rem',
                borderRadius: '4px',
                border: '1px solid var(--color-extension-input-border)',
                fontSize: '14px',
              }}
            />
            <button
              onClick={handleImport}
              disabled={loading || !isConnected || !folderPath.trim()}
              className="extensions-panel__import-button"
            >
              {loading ? t('extensions.importing') : t('extensions.importButton')}
            </button>
          </div>
          <span className="extensions-panel__import-hint">
            {t('extensions.importHint')}
          </span>
        </div>
      </div>

      {/* 扩展列表 */}
      <div className="extensions-panel__list">
        {loading && extensions.length === 0 && (
          <div className="extensions-panel__loading">
            {t('common.loading')}
          </div>
        )}

        {!loading && extensions.length === 0 && (
          <div className="extensions-panel__empty">
            {t('extensions.noExtensions')}
          </div>
        )}

        {extensions.length > 0 && (
          <div>
            {extensions.map((ext) => (
              <div key={ext.name} className="extensions-panel__item">
                <div className="extensions-panel__item-content">
                  <div className="extensions-panel__item-header">
                    <span className="extensions-panel__item-name">
                      {ext.name}
                    </span>
                    <span className="extensions-panel__item-class">
                      {ext.class_name}
                    </span>
                  </div>
                  {ext.description && (
                    <p className="extensions-panel__item-description">
                      {ext.description}
                    </p>
                  )}
                </div>

                <div className="extensions-panel__item-actions">
                  {/* 启用/禁用切换 */}
                  <label className="extensions-panel__toggle">
                    <input
                      type="checkbox"
                      checked={ext.enabled}
                      onChange={(e) =>
                        handleToggle(ext.name, e.target.checked)
                      }
                      disabled={loading || !isConnected}
                      className="extensions-panel__toggle-input"
                    />
                    <div
                      className={`extensions-panel__toggle-track ${
                        ext.enabled ? 'enabled' : ''
                      }`}
                    >
                      <div className="extensions-panel__toggle-thumb" />
                    </div>
                  </label>

                  {/* 删除按钮 */}
                  <button
                    onClick={() => handleDelete(ext.name)}
                    disabled={loading || !isConnected}
                    className="extensions-panel__delete-button"
                  >
                    {t('extensions.deleteButton')}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 帮助提示 */}
      <div className="extensions-panel__help">
        <h4 className="extensions-panel__help-title">
          {t('extensions.helpTitle')}
        </h4>
        <ul className="extensions-panel__help-list">
          <li>{t('extensions.help0')}</li>
          <li>{t('extensions.help1')}</li>
          <li>{t('extensions.help2')}</li>
        </ul>
      </div>
    </div>
  );
}
