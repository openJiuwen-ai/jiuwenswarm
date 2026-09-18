import { createPortal } from 'react-dom';
import { useMemo, useRef, useState, type RefObject } from 'react';
import { Check, Search, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  getAgentAvatarUrl,
  isAgentGroupAgentSelectable,
  resolveAgentGroupSelectionId,
  type AgentCatalogItem,
} from '../../features/agentManagement';
import { PageCard } from '../ui';
import { useDialogFocusTrap } from './useDialogFocusTrap';

export function AgentOptionAvatar({ agent }: { agent: AgentCatalogItem }) {
  const [imageFailed, setImageFailed] = useState(false);
  const avatarUrl = agent.avatarUrl && !imageFailed ? agent.avatarUrl : null;
  return (
    <span className="agent-management-capability-card__icon agent-management-selection-avatar" aria-hidden="true">
      {avatarUrl ? (
        <img src={avatarUrl} alt="" onError={() => setImageFailed(true)} />
      ) : (
        <span>{agent.displayName.trim().slice(0, 1).toUpperCase() || '?'}</span>
      )}
    </span>
  );
}

type AgentGroupMemberPickerProps = {
  mode: 'leader' | 'member';
  agents: AgentCatalogItem[];
  selectedLeaderId: string;
  selectedMemberIds: string[];
  onCancel: () => void;
  onConfirm: (ids: string[]) => void;
  restoreFocusRef?: RefObject<HTMLElement | null>;
};

export function AgentGroupMemberPicker({
  mode,
  agents,
  selectedLeaderId,
  selectedMemberIds,
  onCancel,
  onConfirm,
  restoreFocusRef,
}: AgentGroupMemberPickerProps) {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const [selection, setSelection] = useState<string[]>(
    mode === 'leader' ? (selectedLeaderId ? [selectedLeaderId] : []) : selectedMemberIds,
  );
  const dialogRef = useRef<HTMLElement>(null);
  const filteredAgents = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return agents.filter((agent) => {
      const selectionId = resolveAgentGroupSelectionId(agent);
      const selectable = isAgentGroupAgentSelectable(agent, mode);
      const selected =
        mode === 'leader'
          ? selection.includes(selectionId) || selection.includes(agent.id)
          : selection.includes(selectionId) ||
            selection.includes(agent.id) ||
            selectionId === selectedLeaderId ||
            agent.id === selectedLeaderId;
      if (!selectable && !selected) return false;
      if (!normalized) return true;
      return `${agent.id} ${agent.runtimePackageName} ${agent.displayName} ${agent.description} ${agent.category} ${agent.tags.map((tag) => tag.label).join(' ')}`
        .toLocaleLowerCase()
        .includes(normalized);
    });
  }, [agents, mode, query, selectedLeaderId, selection]);

  useDialogFocusTrap({ dialogRef, restoreFocusRef, onEscape: onCancel });

  const toggle = (id: string, legacyId?: string) => {
    if (mode === 'leader') {
      setSelection([id]);
      return;
    }
    setSelection((current) => {
      const selected = current.includes(id) || (legacyId ? current.includes(legacyId) : false);
      if (selected) {
        return current.filter((item) => item !== id && item !== legacyId);
      }
      return [...current, id];
    });
  };

  const title =
    mode === 'leader' ? t('agentManagement.group.picker.leaderTitle') : t('agentManagement.group.picker.memberTitle');
  const selectedCount = selection.length;

  return createPortal(
    <div
      className="agent-management-selection-overlay"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      <section
        ref={dialogRef}
        className="agent-management-selection-dialog"
        data-testid="agent-group-member-picker"
        role="dialog"
        aria-modal="true"
        aria-labelledby="agent-group-picker-title"
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <h2 id="agent-group-picker-title">{title}</h2>
          <button
            type="button"
            aria-label={t('common.close')}
            data-testid="agent-group-member-picker-close"
            onClick={onCancel}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>
        <label className="agent-management-selection-search">
          <Search size={16} aria-hidden="true" />
          <input
            type="search"
            data-testid="agent-group-member-picker-search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t('agentManagement.form.selectionSearchPlaceholder')}
            autoFocus
          />
        </label>
        <div
          className={`agent-management-selection-dialog__body${filteredAgents.length === 0 ? ' is-empty' : ''}`}
          role="group"
          aria-label={title}
        >
          {filteredAgents.length === 0 ? (
            <div className="agent-management-selection-empty-state">
              <p>{t('agentManagement.group.picker.empty')}</p>
            </div>
          ) : (
            <div className="agent-management-selection-grid">
              {filteredAgents.map((agent) => {
                const selectionId = resolveAgentGroupSelectionId(agent);
                const selected = selection.includes(selectionId) || selection.includes(agent.id);
                const selectable = isAgentGroupAgentSelectable(agent, mode);
                const disabled =
                  !selectable ||
                  (mode === 'member'
                    ? selectionId === selectedLeaderId || agent.id === selectedLeaderId
                    : selectedMemberIds.includes(selectionId) || selectedMemberIds.includes(agent.id));
                const categoryTags = agent.tags.length > 0 ? agent.tags.map((tag) => tag.label) : undefined;
                const description = agent.description || t('agentManagement.unknownDescription');
                return (
                  <PageCard
                    key={agent.id}
                    className={`agent-management-selection-card agent-management-selection-card--expert${selected ? ' is-selected' : ''}${disabled ? ' is-disabled' : ''}`}
                    testId="agent-group-member-picker-item"
                    variant={selectionId}
                    interactive
                    selected={selected}
                    disabled={disabled}
                    ariaLabel={agent.displayName}
                    onClick={() => toggle(selectionId, agent.id)}
                    avatar={{
                      name: agent.displayName,
                      iconUrl: getAgentAvatarUrl(agent),
                      testId: 'agent-group-member-picker-avatar',
                    }}
                    title={agent.displayName}
                    label={categoryTags}
                    description={description}
                    actionSlot={(
                      <span className="agent-management-selection-card__action" aria-hidden="true">
                        {selected ? <Check size={12} strokeWidth={2.5} /> : null}
                      </span>
                    )}
                  />
                );
              })}
            </div>
          )}
        </div>
        <footer>
          <span>{t('agentManagement.group.picker.selectedCount', { count: selectedCount })}</span>
          <div>
            <button
              type="button"
              data-testid="agent-group-member-picker-cancel"
              className="agent-management-button agent-management-button--secondary"
              onClick={onCancel}
            >
              {t('common.cancel')}
            </button>
            <button
              type="button"
              data-testid="agent-group-member-picker-confirm"
              className="agent-management-button agent-management-button--primary"
              disabled={mode === 'leader' && selection.length === 0}
              onClick={() =>
                onConfirm(
                  Array.from(
                    new Set(
                      selection.map((id) => {
                        const agent = agents.find(
                          (item) => resolveAgentGroupSelectionId(item) === id || item.id === id,
                        );
                        return agent ? resolveAgentGroupSelectionId(agent) : id;
                      }),
                    ),
                  ),
                )
              }
            >
              {t('common.confirm')}
            </button>
          </div>
        </footer>
      </section>
    </div>,
    document.body,
  );
}
