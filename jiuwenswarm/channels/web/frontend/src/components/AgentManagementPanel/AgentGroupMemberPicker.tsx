import { useMemo, useState } from 'react';
import { LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import EntityAddIcon from '../../assets/agent-management/add.svg?react';
import EntityRemoveIcon from '../../assets/agent-management/remove.svg?react';
import {
  getAgentAvatarUrl,
  isAgentGroupAgentCompatibilityLoading,
  isAgentGroupAgentSelectable,
  resolveAgentGroupSelectionId,
  sortAgentGroupOptions,
  type AgentCatalogItem,
  type RequestStatus,
} from '../../features/agentManagement';
import { FormDrawer, PageCard, SelectedCount, Tabs } from '../ui';
import type { PageCardDefaultButton } from '../ui';

type AgentGroupMemberPickerProps = {
  mode: 'leader' | 'member';
  agents: AgentCatalogItem[];
  agentsStatus: RequestStatus;
  agentsError: string | null;
  selectedLeaderId: string;
  selectedMemberIds: string[];
  onCancel: () => void;
  onConfirm: (ids: string[]) => void;
  onReloadAgents: () => void;
  onInstallAgent?: (id: string) => void | Promise<void>;
  installingAgentIds?: ReadonlySet<string>;
  restoreFocusRef?: { current: HTMLElement | null };
};

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

export function AgentGroupMemberPicker({
  mode,
  agents,
  agentsStatus,
  agentsError,
  selectedLeaderId,
  selectedMemberIds,
  onCancel,
  onConfirm,
  onReloadAgents,
  onInstallAgent,
  installingAgentIds,
}: AgentGroupMemberPickerProps) {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const [sourceTab, setSourceTab] = useState<'local' | 'market'>('market');
  const [selection, setSelection] = useState<string[]>(
    mode === 'leader' ? (selectedLeaderId ? [selectedLeaderId] : []) : selectedMemberIds,
  );
  const filteredAgents = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    const sourceAgents = agents.filter((agent) =>
      sourceTab === 'market' ? agent.source !== 'local' : agent.source === 'local' || agent.installed === true,
    );
    return sortAgentGroupOptions(sourceAgents, agentsStatus).filter((agent) => {
      if (!normalized) return true;
      return `${agent.id} ${agent.runtimePackageName} ${agent.displayName} ${agent.description} ${agent.category} ${agent.tags.map((tag) => tag.label).join(' ')}`
        .toLocaleLowerCase()
        .includes(normalized);
    });
  }, [agents, agentsStatus, query, sourceTab]);

  const toggle = (id: string, legacyId?: string) => {
    if (mode === 'leader') {
      // 单选但可反选：再点已选中的即取消，确认按钮会因 selection 为空而置灰
      setSelection((current) =>
        current.includes(id) || (legacyId ? current.includes(legacyId) : false) ? [] : [id],
      );
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

  const normalizeSelection = () =>
    Array.from(
      new Set(
        selection.map((id) => {
          const agent = agents.find((item) => resolveAgentGroupSelectionId(item) === id || item.id === id);
          return agent ? resolveAgentGroupSelectionId(agent) : id;
        }),
      ),
    );

  return (
    <FormDrawer
      title={title}
      onClose={onCancel}
      onConfirm={() => onConfirm(normalizeSelection())}
      confirmDisabled={mode === 'leader' && selection.length === 0}
      testId="agent-group-member-picker"
      width={900}
      bodyClassName="form-drawer__body--flush"
      footerLeading={
        <SelectedCount count={selection.length} testId="agent-group-member-picker-selected-count" />
      }
    >
      <div className="relative mx-6 mb-4 shrink-0">
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t('agentManagement.form.selectionSearchPlaceholder')}
          className="h-8 w-full rounded-lg border border-border bg-bg pl-8 pr-3 text-[12px] leading-[18px] text-text outline-none focus:border-border-hover"
          data-testid="agent-group-member-picker-search"
        />
      </div>
      <Tabs
        className="mb-4 px-6"
        items={[
          {
            value: 'market',
            label: t('agentManagement.tabs.catalog'),
            testId: 'agent-group-member-picker-tab-market',
          },
          {
            value: 'local',
            label: t('agentManagement.tabs.mine'),
            testId: 'agent-group-member-picker-tab-local',
          },
        ]}
        value={sourceTab}
        onChange={(value) => {
          setSourceTab(value);
        }}
        wrapperTestId="agent-group-member-picker-tabs"
        role="tablist"
        ariaLabel={t('agentManagement.group.picker.sourceTabsLabel')}
      />
      <div className="form-drawer__scroll-area">
        {agentsStatus === 'error' ? (
          <div className="agent-management-form-error" role="alert" data-testid="agent-group-member-picker-error">
            <span>{agentsError || t('agentManagement.states.loadError')}</span>
            <button type="button" onClick={onReloadAgents}>
              {t('common.retry')}
            </button>
          </div>
        ) : agentsStatus === 'loading' && agents.length === 0 ? (
          <div className="py-10 text-center text-[13px] text-text-muted">
            {t('agentManagement.group.picker.loading')}
          </div>
        ) : filteredAgents.length === 0 ? (
          <div className="py-10 text-center text-[13px] text-text-muted">
            <p>{t('agentManagement.group.picker.empty')}</p>
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-4" data-testid="agent-group-member-picker-list">
            {filteredAgents.map((agent) => {
              const selectionId = resolveAgentGroupSelectionId(agent);
              const selected = selection.includes(selectionId) || selection.includes(agent.id);
              const compatibilityLoading = isAgentGroupAgentCompatibilityLoading(agent, agentsStatus);
              const selectable = !compatibilityLoading && isAgentGroupAgentSelectable(agent, mode);
              const disabled =
                !selectable ||
                (mode === 'member'
                  ? selectionId === selectedLeaderId || agent.id === selectedLeaderId
                  : selectedMemberIds.includes(selectionId) || selectedMemberIds.includes(agent.id));
              const installed = agent.installed === true;
              const installing = installingAgentIds?.has(agent.id) === true;
              const categoryTags = agent.tags.length > 0 ? agent.tags.map((tag) => tag.label) : undefined;
              const description = agent.description || t('agentManagement.unknownDescription');
              const cardDisabled = !compatibilityLoading && disabled;
              const defaultButton: PageCardDefaultButton | undefined =
                !compatibilityLoading && !installed && onInstallAgent
                  ? {
                      text: installing
                        ? t('agentManagement.group.picker.installing')
                        : t('agentManagement.group.picker.install'),
                      testId: 'agent-group-member-picker-install',
                      variant: agent.id,
                      disabled: installing,
                      busy: installing,
                      onClick: () => void onInstallAgent(agent.id),
                    }
                  : undefined;
              return (
                <PageCard
                  key={agent.id}
                  className={`${selected ? ' is-selected' : ''}${compatibilityLoading ? ' is-loading' : cardDisabled ? ' is-disabled' : ''}`}
                  testId="agent-group-member-picker-item"
                  variant={selectionId}
                  interactive={installed && !compatibilityLoading}
                  selected={selected}
                  disabled={cardDisabled && installed}
                  onClick={
                    installed && !compatibilityLoading && !disabled ? () => toggle(selectionId, agent.id) : undefined
                  }
                  avatar={{
                    name: agent.displayName,
                    iconUrl: getAgentAvatarUrl(agent),
                    testId: 'agent-group-member-picker-avatar',
                  }}
                  title={agent.displayName}
                  label={categoryTags}
                  description={description}
                  defaultButton={defaultButton}
                  actionSlot={
                    compatibilityLoading ? (
                      <span className="animate-spin" aria-label={t('agentManagement.group.picker.loading')}>
                        <LoaderCircle size={16} />
                      </span>
                    ) : installed || !onInstallAgent ? (
                      <span className="shrink-0" aria-hidden="true">
                        {selected ? (
                          <EntityRemoveIcon className="text-[color:var(--color-chat-accent)]" />
                        ) : (
                          <EntityAddIcon className="text-text-muted" />
                        )}
                      </span>
                    ) : null
                  }
                />
              );
            })}
          </div>
        )}
      </div>
    </FormDrawer>
  );
}
