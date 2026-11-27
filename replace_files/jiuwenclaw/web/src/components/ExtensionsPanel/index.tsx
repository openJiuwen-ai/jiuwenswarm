import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import './ExtensionsPanel.css';

interface RailExtension {
  name: string;
  class_name: string;
  enabled: boolean;
  description: string;
  priority: number;
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
        throw new Error('加载扩展列表失败');
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
      setError('请输入扩展文件夹路径');
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
        throw new Error('导入扩展失败');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [folderPath]);

  const handleDelete = useCallback(
    async (name: string) => {
      if (!confirm(`确定要删除扩展 "${name}" 吗？`)) {
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
          // 确认后端更新成功，使用返回的数据
          console.log("===> extension:", extension)
          setExtensions(prev =>
            prev.map(ext => (ext.name === name ? extension : ext))
          );
        } else {
          throw new Error('切换扩展状态失败');
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
        {t('extensions.title', '能力扩展')}
      </div>
      <p className="extensions-panel__description">
        {t('extensions.description', '管理自定义 Rail 扩展')}
      </p>

      {error && (
        <div className="extensions-panel__error">
          {error}
        </div>
      )}

      {/* 导入区域 */}
      <div className="extensions-panel__import-section">
        <h3 className="extensions-panel__import-title">
          {t('extensions.importTitle', '导入能力扩展')}
        </h3>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
          <div style={{ display: 'flex', gap: '0.5rem' }}>
            <input
              type="text"
              value={folderPath}
              onChange={(e) => setFolderPath(e.target.value)}
              placeholder="输入扩展文件夹路径 (例如: D:/extensions/my_extension)"
              disabled={loading || !isConnected}
              style={{
                flex: 1,
                padding: '0.5rem',
                borderRadius: '4px',
                border: '1px solid #ccc',
                fontSize: '14px',
              }}
            />
            <button
              onClick={handleImport}
              disabled={loading || !isConnected || !folderPath.trim()}
              className="extensions-panel__import-button"
            >
              {loading ? '导入中...' : t('extensions.importButton', '导入')}
            </button>
          </div>
          <span className="extensions-panel__import-hint">
            {t('extensions.importHint', '文件夹要求：1) 英文名称 2) 包含 rail.py 入口文件')}
          </span>
        </div>
      </div>

      {/* 扩展列表 */}
      <div className="extensions-panel__list">
        {loading && extensions.length === 0 && (
          <div className="extensions-panel__loading">
            {t('common.loading', '加载中...')}
          </div>
        )}

        {!loading && extensions.length === 0 && (
          <div className="extensions-panel__empty">
            {t('extensions.noExtensions', '暂无能力扩展')}
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
                    {t('extensions.deleteButton', '删除')}
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
          {t('extensions.helpTitle', '使用说明')}
        </h4>
        <ul className="extensions-panel__help-list">
          <li>
            {t('extensions.help0', '扩展文件夹必须包含 rail.py 作为入口文件')}
          </li>
          <li>
            {t(
              'extensions.help1',
              'Rail 扩展文件必须继承 DeepAgentRail 或 AgentRail 基类'
            )}
          </li>
          <li>
            {t(
              'extensions.help2',
              '扩展文件夹会被保存到 ~/.jiuwenclaw/agent/jiuwenclaw_workspace/extensions/ 目录'
            )}
          </li>
        </ul>
      </div>
    </div>
  );
}
