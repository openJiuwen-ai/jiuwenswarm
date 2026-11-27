import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { EditableCombobox } from './EditableCombobox';
import { isClickOutside } from './clickOutside';
import { agentContextKey, useEnterpriseContext } from '../../services/enterpriseContext';

export function EnterpriseContextSwitcher() {
  const { t } = useTranslation();
  const enterprise = useEnterpriseContext();
  const [contextOpen, setContextOpen] = useState(false);
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

  const agentContextOptions = useMemo(
    () =>
      (enterprise?.contexts ?? []).map(item => ({
        value: agentContextKey(item),
        label: item.agent_name?.trim() || item.bot_id,
      })),
    [enterprise?.contexts],
  );

  if (!enterprise) return null;
  const displayName = enterprise.user.display_name || enterprise.user.user_id;
  const selectedKey = agentContextKey(enterprise.selected);
  const selectedInContexts = enterprise.contexts.some(item => agentContextKey(item) === selectedKey);

  return (
    <>
      <button
        ref={contextButtonRef}
        type="button"
        className={`icon-rail-nav-item${contextOpen ? ' icon-rail-nav-item--active' : ''}`}
        onClick={() => setContextOpen(open => !open)}
        aria-label={t('sessionSidebar.enterpriseContext.userContext')}
        title={t('sessionSidebar.enterpriseContext.userContext')}
        data-testid="session-sidebar-enterprise-context-button"
      >
        <span className="icon-rail-nav-item__icon">{displayName.slice(0, 1).toUpperCase()}</span>
        <span className="icon-rail-nav-item__label">{t('sessionSidebar.enterpriseContext.user')}</span>
      </button>
      {contextOpen && (
        <div ref={contextPanelRef} className="enterprise-context-popover" data-testid="session-sidebar-enterprise-context-popover">
          <div className="enterprise-context-popover__user">
            {displayName}
            <small>{enterprise.user.user_id}</small>
          </div>
          <EditableCombobox
            ariaLabel={t('sessionSidebar.enterpriseContext.agentNameLabel')}
            emptyText={t('sessionSidebar.enterpriseContext.noMatch')}
            placeholder={t('sessionSidebar.enterpriseContext.pleaseSelect')}
            strictSelection
            value={selectedInContexts ? selectedKey : ''}
            disabled={enterprise.contextSwitching}
            options={agentContextOptions}
            onChange={key => enterprise.onContextChange(key)}
          />
          <div
            className="enterprise-context-popover__current-agent"
            data-testid="session-sidebar-enterprise-agent-name-hint"
          >
            {t('sessionSidebar.enterpriseContext.agentNameHint')}
          </div>
          {enterprise.contextSwitching && (
            <div
              className="enterprise-context-popover__status"
              data-testid="session-sidebar-enterprise-context-status"
            >
              {t('sessionSidebar.enterpriseContext.switching')}
            </div>
          )}
          {enterprise.contextError && (
            <div
              className="enterprise-context-popover__error"
              data-testid="session-sidebar-enterprise-context-error"
            >
              {enterprise.contextError}
            </div>
          )}
          <button
            type="button"
            className="enterprise-context-popover__logout"
            onClick={enterprise.onLogout}
            data-testid="session-sidebar-enterprise-context-logout"
          >
            {t('sessionSidebar.enterpriseContext.logout')}
          </button>
        </div>
      )}
    </>
  );
}
