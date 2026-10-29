import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { EditableCombobox } from './EditableCombobox';
import { isClickOutside } from './clickOutside';
import {
  agentContextKey,
  formatAgentContextLabel,
  useEnterpriseContext,
} from '../../services/enterpriseContext';

type ContextMode = 'select' | 'custom';

export function EnterpriseContextSwitcher() {
  const { t } = useTranslation();
  const enterprise = useEnterpriseContext();
  const [contextOpen, setContextOpen] = useState(false);
  const [contextMode, setContextMode] = useState<ContextMode>('select');
  const [listSelectionKey, setListSelectionKey] = useState('');
  const [customBotId, setCustomBotId] = useState('');
  const [customGroupId, setCustomGroupId] = useState('');
  const [customUserId, setCustomUserId] = useState('');
  const [customJiuwenclawId, setCustomJiuwenclawId] = useState('');
  const contextButtonRef = useRef<HTMLButtonElement>(null);
  const contextPanelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!contextOpen) return;
    const handleClickOutside = (event: MouseEvent) => {
      if (isClickOutside(event.target as Node | null, [contextPanelRef.current, contextButtonRef.current])) {
        setContextOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [contextOpen]);

  useEffect(() => {
    if (!enterprise || !contextOpen) return;
    setCustomBotId(enterprise.selected.bot_id);
    setCustomGroupId(enterprise.selected.group_id);
    setCustomUserId(enterprise.selected.user_id);
    setCustomJiuwenclawId(enterprise.selected.jiuwenclaw_id);
  }, [contextOpen, enterprise]);

  const activeMode: ContextMode = useMemo(() => {
    if (!enterprise) return 'select';
    const selectedKey = agentContextKey(enterprise.selected);
    return enterprise.contexts.some(item => agentContextKey(item) === selectedKey) ? 'select' : 'custom';
  }, [enterprise]);

  useLayoutEffect(() => {
    setContextMode(activeMode);
  }, [activeMode]);

  useEffect(() => {
    if (!enterprise?.contexts.length) return;
    const selectedKey = agentContextKey(enterprise.selected);
    if (enterprise.contexts.some(item => agentContextKey(item) === selectedKey)) {
      setListSelectionKey(selectedKey);
      return;
    }
    setListSelectionKey('');
  }, [enterprise]);

  const agentContextOptions = useMemo(
    () =>
      (enterprise?.contexts ?? []).map(item => ({
        value: agentContextKey(item),
        label: formatAgentContextLabel(item),
      })),
    [enterprise?.contexts],
  );

  if (!enterprise) return null;
  const displayName = enterprise.user.display_name || enterprise.user.user_id;

  return (
    <>
      <button
        ref={contextButtonRef}
        type="button"
        className={`icon-rail-nav-item${contextOpen ? ' icon-rail-nav-item--active' : ''}`}
        onClick={() => setContextOpen(open => !open)}
        aria-label={t('sessionSidebar.enterpriseContext.userContext')}
        title={t('sessionSidebar.enterpriseContext.userContext')}
        data-testid="enterprise-context-button"
      >
        <span className="icon-rail-nav-item__icon">{displayName.slice(0, 1).toUpperCase()}</span>
        <span className="icon-rail-nav-item__label">{t('sessionSidebar.enterpriseContext.user')}</span>
      </button>
      {contextOpen && (
        <div ref={contextPanelRef} className="enterprise-context-popover" data-testid="enterprise-context-popover">
          <div className="enterprise-context-popover__user">
            {displayName}
            <small>{enterprise.user.user_id}</small>
          </div>
          <div className="enterprise-context-popover__modes" role="tablist" aria-label={t('sessionSidebar.enterpriseContext.modeAria')}>
            <button
              type="button"
              role="tab"
              aria-selected={contextMode === 'select'}
              className={`enterprise-context-popover__mode${contextMode === 'select' ? ' enterprise-context-popover__mode--active' : ''}`}
              disabled={enterprise.contextSwitching}
              onClick={() => {
                if (activeMode === 'custom') setListSelectionKey('');
                setContextMode('select');
              }}
            >
              {t('sessionSidebar.enterpriseContext.selectAgent')}
              {activeMode === 'select' && (
                <span className="enterprise-context-popover__mode-check" aria-hidden>✓</span>
              )}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={contextMode === 'custom'}
              className={`enterprise-context-popover__mode${contextMode === 'custom' ? ' enterprise-context-popover__mode--active' : ''}`}
              disabled={enterprise.contextSwitching}
              onClick={() => setContextMode('custom')}
            >
              {t('sessionSidebar.enterpriseContext.custom')}
              {activeMode === 'custom' && (
                <span className="enterprise-context-popover__mode-check" aria-hidden>✓</span>
              )}
            </button>
          </div>
          {contextMode === 'select' ? (
            <label>
              {t('sessionSidebar.enterpriseContext.agentOrgLabel')}
              <EditableCombobox
                ariaLabel={t('sessionSidebar.enterpriseContext.agentOrgLabel')}
                emptyText={t('sessionSidebar.enterpriseContext.noMatch')}
                placeholder={t('sessionSidebar.enterpriseContext.pleaseSelect')}
                value={listSelectionKey}
                disabled={enterprise.contextSwitching}
                options={agentContextOptions}
                onChange={key => {
                  setListSelectionKey(key);
                  enterprise.onContextChange(key);
                }}
              />
            </label>
          ) : (
            <>
              <label>
                {t('sessionSidebar.enterpriseContext.botId')}
                <input className="enterprise-context-popover__input" value={customBotId} disabled={enterprise.contextSwitching} onChange={event => setCustomBotId(event.target.value)} aria-label={t('sessionSidebar.enterpriseContext.botId')} />
              </label>
              <label>
                {t('sessionSidebar.enterpriseContext.groupId')}
                <input className="enterprise-context-popover__input" value={customGroupId} disabled={enterprise.contextSwitching} onChange={event => setCustomGroupId(event.target.value)} aria-label={t('sessionSidebar.enterpriseContext.groupId')} />
              </label>
              <label>
                {t('sessionSidebar.enterpriseContext.userId')}
                <input className="enterprise-context-popover__input" value={customUserId} disabled={enterprise.contextSwitching} onChange={event => setCustomUserId(event.target.value)} aria-label={t('sessionSidebar.enterpriseContext.userId')} />
              </label>
              <label>
                {t('sessionSidebar.enterpriseContext.jiuwenclawId')}
                <input className="enterprise-context-popover__input" value={customJiuwenclawId} disabled={enterprise.contextSwitching} onChange={event => setCustomJiuwenclawId(event.target.value)} aria-label={t('sessionSidebar.enterpriseContext.jiuwenclawId')} />
              </label>
              <button
                type="button"
                className="enterprise-context-popover__apply"
                disabled={enterprise.contextSwitching}
                onClick={() => enterprise.onCustomContextApply({
                  botId: customBotId,
                  groupId: customGroupId,
                  userId: customUserId,
                  jiuwenclawId: customJiuwenclawId,
                })}
              >
                {t('sessionSidebar.enterpriseContext.apply')}
              </button>
            </>
          )}
          {enterprise.contextSwitching && (
            <div className="enterprise-context-popover__status">{t('sessionSidebar.enterpriseContext.switching')}</div>
          )}
          {enterprise.contextError && <div className="enterprise-context-popover__error">{enterprise.contextError}</div>}
          {enterprise.managerAccess && (
            <button type="button" className="enterprise-context-popover__switch" onClick={enterprise.onSwitchToManager}>
              {t('sessionSidebar.enterpriseContext.switchToManager')}
            </button>
          )}
          <button type="button" className="enterprise-context-popover__logout" onClick={enterprise.onLogout}>
            {t('sessionSidebar.enterpriseContext.logout')}
          </button>
        </div>
      )}
    </>
  );
}
