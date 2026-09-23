import { useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { ArrowLeftRight, Check, ChevronDown, ChevronUp, Minus, Plus, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  dedupeAgentGroupOptions,
  getAgentAvatarUrl,
  isSkillVisibleInSourceTab,
  isTeamSkillOption,
  resolveAgentGroupSelectionId,
  sortInstalledFirst,
  type AgentCatalogItem,
  type AgentGroupDraft,
  type RequestStatus,
  type SkillOption,
} from '../../features/agentManagement';
import { AGENT_DESCRIPTION_MAX_LENGTH, AGENT_NAME_MAX_LENGTH } from '../../features/agentManagement/limits';
import { AgentGroupMemberPicker } from './AgentGroupMemberPicker';
import { AgentTagPicker } from './AgentTagPicker';
import { PageCard, Tabs, Input, Textarea, FormDrawer } from '../ui';
import { FormPageLayout } from '../ConnectorMarket/FormPageLayout';

type AgentGroupEditorProps = {
  draft: AgentGroupDraft;
  agentOptions: AgentCatalogItem[];
  agentsStatus: RequestStatus;
  agentsError: string | null;
  skillOptions: SkillOption[];
  skillsStatus: RequestStatus;
  saving: boolean;
  error: string | null;
  selectionError?: string | null;
  onChange: (draft: AgentGroupDraft) => void;
  onReloadAgents: () => void;
  onReloadSkills: () => void;
  onInstallAgent?: (id: string) => void | Promise<void>;
  installingAgentIds?: ReadonlySet<string>;
  onInstallSkill?: (skill: SkillOption) => void | Promise<void>;
  installingSkillId?: string | null;
  onCreateAgent?: () => void;
  onCancel: () => void;
  onSave: () => void;
};

export function AgentGroupEditor({
  draft,
  agentOptions,
  agentsStatus,
  agentsError,
  skillOptions,
  skillsStatus,
  saving,
  error,
  selectionError,
  onChange,
  onReloadAgents,
  onReloadSkills,
  onInstallAgent,
  installingAgentIds,
  onInstallSkill,
  installingSkillId,
  onCreateAgent,
  onCancel,
  onSave,
}: AgentGroupEditorProps) {
  const { t } = useTranslation();
  const [touched, setTouched] = useState(false);
  const [pickerMode, setPickerMode] = useState<'leader' | 'member' | null>(null);
  const [skillPickerOpen, setSkillPickerOpen] = useState(false);
  const [skillQuery, setSkillQuery] = useState('');
  const [skillSourceTab, setSkillSourceTab] = useState<'local' | 'market'>('market');
  const [skillDraft, setSkillDraft] = useState<string[]>(draft.skillRefs);
  const [teamConfigOpen, setTeamConfigOpen] = useState(true);
  const [skillsOpen, setSkillsOpen] = useState(true);
  const [promptsOpen, setPromptsOpen] = useState(true);
  const leaderPickerTriggerRef = useRef<HTMLElement | null>(null);
  const memberPickerTriggerRef = useRef<HTMLElement | null>(null);
  const promptKeySeedRef = useMemo(() => ({ current: 0 }), []);
  const promptKeysRef = useMemo(() => ({ current: [] as string[] }), []);

  const errors = useMemo(
    () => ({
      name: !draft.name.trim() ? t('agentManagement.group.form.errors.nameRequired') : '',
      description: !draft.description.trim() ? t('agentManagement.group.form.errors.descriptionRequired') : '',
      persona: !draft.persona.trim() ? t('agentManagement.group.form.errors.personaRequired') : '',
      leader: !draft.leaderId ? t('agentManagement.group.form.errors.leaderRequired') : '',
      members: draft.memberIds.length === 0 ? t('agentManagement.group.form.errors.membersRequired') : '',
    }),
    [draft.description, draft.leaderId, draft.memberIds.length, draft.name, draft.persona, t],
  );
  const hasErrors = Object.values(errors).some(Boolean);
  const uniqueAgentOptions = useMemo(() => dedupeAgentGroupOptions(agentOptions), [agentOptions]);
  const selectedLeader = uniqueAgentOptions.find(
    (agent) => resolveAgentGroupSelectionId(agent) === draft.leaderId || agent.id === draft.leaderId,
  );
  const selectedMembers = uniqueAgentOptions.filter(
    (agent) => draft.memberIds.includes(resolveAgentGroupSelectionId(agent)) || draft.memberIds.includes(agent.id),
  );
  const filteredSkills = sortInstalledFirst(
    skillOptions.filter((skill) => {
      if (!isTeamSkillOption(skill, skillSourceTab) || !isSkillVisibleInSourceTab(skill, skillSourceTab)) return false;
      return `${skill.id} ${skill.name} ${skill.description}`
        .toLocaleLowerCase()
        .includes(skillQuery.trim().toLocaleLowerCase());
    }),
  );
  const update = (patch: Partial<AgentGroupDraft>) => onChange({ ...draft, ...patch });

  const nextPromptKey = () => {
    promptKeySeedRef.current += 1;
    return `prompt-${promptKeySeedRef.current}`;
  };
  const promptKeyAt = (index: number) => {
    promptKeysRef.current[index] ||= nextPromptKey();
    return promptKeysRef.current[index];
  };
  const openSkills = () => {
    setSkillDraft(draft.skillRefs);
    setSkillQuery('');
    setSkillSourceTab('market');
    setSkillPickerOpen(true);
  };
  const toggleSkill = (id: string) =>
    setSkillDraft((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
  const addPrompt = () => {
    if (draft.suggestedPrompts.some((prompt) => prompt.trim().length === 0)) return;
    promptKeysRef.current.push(nextPromptKey());
    update({ suggestedPrompts: [...draft.suggestedPrompts, ''] });
  };
  const updatePrompt = (index: number, value: string) =>
    update({
      suggestedPrompts: draft.suggestedPrompts.map((prompt, promptIndex) => (promptIndex === index ? value : prompt)),
    });
  const removePrompt = (index: number) => {
    promptKeysRef.current.splice(index, 1);
    update({ suggestedPrompts: draft.suggestedPrompts.filter((_, promptIndex) => promptIndex !== index) });
  };
  const openMemberPicker = (mode: 'leader' | 'member') => {
    if (agentsStatus !== 'loading' && uniqueAgentOptions.length === 0) onReloadAgents();
    setPickerMode(mode);
  };
  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (!hasErrors) onSave();
  };

  return (
    <>
      <FormPageLayout
        onBack={onCancel}
        title={
          <div className="flex flex-col gap-2">
            <span>{t('agentManagement.group.form.title')}</span>
            <Tabs
              value="group"
              onChange={() => onCreateAgent?.()}
              items={[
                {
                  value: 'agent',
                  label: t('agentManagement.group.form.createAgentTab'),
                  testId: 'agent-group-editor-agent-tab',
                },
                {
                  value: 'group',
                  label: t('agentManagement.group.form.createGroupTab'),
                  testId: 'agent-group-editor-group-tab',
                },
              ]}
              wrapperTestId="agent-management-editor-tabs"
              itemTestId="agent-management-editor-tab"
              role="tablist"
              ariaLabel={t('agentManagement.form.createTabsLabel')}
            />
          </div>
        }
        testId="agent-group-editor"
        onConfirm={() => {
          setTouched(true);
          if (!hasErrors) onSave();
        }}
        cancelLabel={t('common.cancel')}
        confirmLabel={saving ? t('agentManagement.group.actions.saving') : t('common.confirm')}
        confirmLoading={saving}
        footerSlot={
          error ? (
            <div className="agent-management-form-error" role="alert" data-testid="agent-group-editor-error">
              {error}
            </div>
          ) : null
        }
      >
        <form onSubmit={handleSubmit} data-testid="agent-group-editor-form">
          <Section title={t('agentManagement.group.form.basic')}>
            <div className="mb-4">
              <label className="mb-1.5 block text-[13px] font-medium text-text">
                {t('agentManagement.group.form.nameLabel')}
              </label>
              <Input
                value={draft.name}
                onChange={(value) => update({ name: value })}
                placeholder={t('agentManagement.group.form.namePlaceholder')}
                invalid={Boolean(touched && errors.name)}
                data-testid="agent-group-editor-name"
                maxLength={AGENT_NAME_MAX_LENGTH}
                showCounter
                counterTestId="agent-group-editor-name-counter"
              />
              {touched && errors.name ? (
                <p className="mt-1 text-[11px] leading-4 text-danger" data-testid="agent-group-editor-name-error">
                  {errors.name}
                </p>
              ) : null}
            </div>

            <div className="mb-4">
              <label className="mb-1.5 block text-[13px] font-medium text-text">
                {t('agentManagement.group.form.descriptionLabel')}
              </label>
              <Textarea
                value={draft.description}
                onChange={(value) => update({ description: value })}
                placeholder={t('agentManagement.group.form.descriptionPlaceholder')}
                rows={2}
                invalid={Boolean(touched && errors.description)}
                data-testid="agent-group-editor-description"
                maxLength={AGENT_DESCRIPTION_MAX_LENGTH}
                showCounter
                counterTestId="agent-group-editor-description-counter"
                scrollable
              />
              {touched && errors.description ? (
                <p
                  className="mt-1 text-[11px] leading-4 text-danger"
                  data-testid="agent-group-editor-description-error"
                >
                  {errors.description}
                </p>
              ) : null}
            </div>

            <div className="mb-4">
              <label className="mb-1.5 block text-[13px] font-medium text-text">
                {t('agentManagement.group.form.tagLabel')}
              </label>
              <AgentTagPicker
                tagIds={draft.tagIds}
                customTags={draft.customTags}
                label={t('agentManagement.group.form.tagLabel')}
                placeholder={t('agentManagement.group.form.tagPlaceholder')}
                onChange={(value) => update(value)}
              />
            </div>
          </Section>

          <Section title={t('agentManagement.group.form.teamIntro')} defaultOpen>
            <div className="mb-4">
              <Textarea
                value={draft.persona}
                onChange={(value) => update({ persona: value })}
                placeholder={t('agentManagement.group.form.personaPlaceholder')}
                rows={8}
                invalid={Boolean(touched && errors.persona)}
                aria-label={t('agentManagement.group.form.personaLabel')}
                data-testid="agent-group-editor-persona"
              />
              {touched && errors.persona ? (
                <p className="mt-1 text-[11px] leading-4 text-danger" data-testid="agent-group-editor-persona-error">
                  {errors.persona}
                </p>
              ) : null}
            </div>
          </Section>

          <Section
            title={t('agentManagement.group.form.teamConfig')}
            collapsible
            defaultOpen={teamConfigOpen}
            onToggle={() => setTeamConfigOpen((open) => !open)}
          >
            <div className="mb-4 space-y-3">
              <div>
                <div className="mb-1.5 flex items-center justify-between text-[13px] font-medium text-text">
                  <span>{t('agentManagement.group.form.leaderFieldLabel')}</span>
                  {!selectedLeader ? (
                    <button
                      type="button"
                      className="flex items-center gap-1 rounded-full px-2.5 py-1 text-[13px] text-text hover:text-[color:var(--color-chat-accent)]"
                      data-testid="agent-group-editor-add-leader"
                      onClick={() => openMemberPicker('leader')}
                    >
                      <Plus size={14} />
                      {t('agentManagement.group.form.leaderLabel')}
                    </button>
                  ) : null}
                </div>
                {selectedLeader ? (
                  <PageCard
                    testId="agent-group-editor-leader-card"
                    avatar={{ name: selectedLeader.displayName, iconUrl: getAgentAvatarUrl(selectedLeader) }}
                    title={selectedLeader.displayName}
                    description={selectedLeader.description || t('agentManagement.unknownDescription')}
                    actionsHover
                    action={{
                      icon: <ArrowLeftRight size={15} />,
                      onClick: () => openMemberPicker('leader'),
                    }}
                  />
                ) : touched && errors.leader ? (
                  <p className="text-[11px] leading-4 text-danger" data-testid="agent-group-editor-leader-error">
                    {errors.leader}
                  </p>
                ) : null}
              </div>

              <div>
                <div className="mb-1.5 flex items-center justify-between text-[13px] font-medium text-text">
                  <span>{t('agentManagement.group.form.membersFieldLabel')}</span>
                  <button
                    type="button"
                    className="flex items-center gap-1 rounded-full px-2.5 py-1 text-[13px] text-text hover:text-[color:var(--color-chat-accent)]"
                    data-testid="agent-group-editor-add-member"
                    onClick={() => openMemberPicker('member')}
                  >
                    <Plus size={14} />
                    {t('agentManagement.group.form.addMember')}
                  </button>
                </div>
                {selectedMembers.length > 0 ? (
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    {selectedMembers.map((agent) => (
                      <PageCard
                        key={agent.id}
                        testId="agent-group-editor-member-card"
                        variant={resolveAgentGroupSelectionId(agent)}
                        avatar={{ name: agent.displayName, iconUrl: getAgentAvatarUrl(agent) }}
                        title={agent.displayName}
                        description={agent.description || t('agentManagement.unknownDescription')}
                        actionsHover
                        action={{
                          icon: <Trash2 size={15} />,
                          onClick: () =>
                            update({
                              memberIds: draft.memberIds.filter(
                                (id) => id !== resolveAgentGroupSelectionId(agent) && id !== agent.id,
                              ),
                            }),
                        }}
                      />
                    ))}
                  </div>
                ) : touched && errors.members ? (
                  <p className="text-[11px] leading-4 text-danger" data-testid="agent-group-editor-members-error">
                    {errors.members}
                  </p>
                ) : null}
              </div>
            </div>
          </Section>

          <Section
            title={t('agentManagement.group.form.skillsLabel')}
            action={
              <button
                type="button"
                className="flex items-center gap-1 rounded-full px-2.5 py-1 text-[13px] text-text hover:text-[color:var(--color-chat-accent)]"
                data-testid="agent-group-editor-choose-skills"
                onClick={openSkills}
              >
                <Plus size={14} />
                {t('agentManagement.group.form.chooseSkills')}
              </button>
            }
            collapsible
            defaultOpen={skillsOpen}
            onToggle={() => setSkillsOpen((open) => !open)}
          >
            {draft.skillRefs.length > 0 ? (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                {draft.skillRefs.map((id) => {
                  const skill = skillOptions.find((option) => option.id === id);
                  const name = skill?.name || id;
                  return (
                    <PageCard
                      key={id}
                      testId="agent-group-editor-skill-item"
                      variant={id}
                      avatar={{ name }}
                      title={name}
                      description={skill?.description || t('agentManagement.unknownDescription')}
                      actionsHover
                      action={{
                        icon: <Trash2 size={15} />,
                        onClick: () => update({ skillRefs: draft.skillRefs.filter((item) => item !== id) }),
                      }}
                    />
                  );
                })}
              </div>
            ) : null}
          </Section>

          <Section
            title={t('agentManagement.group.form.promptsLabel')}
            action={
              <button
                type="button"
                className="flex items-center gap-1 rounded-full px-2.5 py-1 text-[13px] text-text hover:text-[color:var(--color-chat-accent)]"
                data-testid="agent-group-editor-add-prompt"
                onClick={addPrompt}
              >
                <Plus size={14} />
                {t('agentManagement.group.form.addPrompt')}
              </button>
            }
            collapsible
            defaultOpen={promptsOpen}
            onToggle={() => setPromptsOpen((open) => !open)}
          >
            {draft.suggestedPrompts.length > 0 ? (
              <div className="space-y-2">
                {draft.suggestedPrompts.map((prompt, index) => (
                  <div className="flex items-center gap-2" key={promptKeyAt(index)}>
                    <Input
                      value={prompt}
                      onChange={(value) => updatePrompt(index, value)}
                      placeholder={t('agentManagement.group.form.promptPlaceholder')}
                      data-testid="agent-group-editor-prompt-input"
                      data-variant={promptKeyAt(index)}
                    />
                    <button
                      type="button"
                      className="shrink-0 rounded-full p-1.5 text-text-muted hover:bg-secondary hover:text-text"
                      data-testid="agent-group-editor-remove-prompt"
                      data-variant={promptKeyAt(index)}
                      aria-label={t('agentManagement.group.form.removePrompt')}
                      onClick={() => removePrompt(index)}
                    >
                      <Minus size={14} />
                    </button>
                  </div>
                ))}
              </div>
            ) : null}
          </Section>
        </form>
      </FormPageLayout>

      {pickerMode ? (
        <AgentGroupMemberPicker
          mode={pickerMode}
          agents={uniqueAgentOptions}
          agentsStatus={agentsStatus}
          agentsError={agentsError}
          selectedLeaderId={draft.leaderId}
          selectedMemberIds={draft.memberIds}
          restoreFocusRef={pickerMode === 'leader' ? leaderPickerTriggerRef : memberPickerTriggerRef}
          selectionError={selectionError}
          onInstallAgent={onInstallAgent}
          installingAgentIds={installingAgentIds}
          onReloadAgents={onReloadAgents}
          onCancel={() => setPickerMode(null)}
          onConfirm={(ids) => {
            if (pickerMode === 'leader')
              update({ leaderId: ids[0] || '', memberIds: draft.memberIds.filter((id) => id !== ids[0]) });
            else update({ memberIds: ids.filter((id) => id !== draft.leaderId) });
            setPickerMode(null);
          }}
        />
      ) : null}

      {skillPickerOpen && (
        <FormDrawer
          title={t('agentManagement.group.form.chooseSkills')}
          onClose={() => setSkillPickerOpen(false)}
          onConfirm={() => {
            update({ skillRefs: skillDraft });
            setSkillPickerOpen(false);
          }}
          testId="agent-group-editor-skill-picker"
          width={900}
        >
          <div className="relative mb-4 shrink-0">
            <input
              value={skillQuery}
              onChange={(event) => setSkillQuery(event.target.value)}
              placeholder={t('agentManagement.form.selectionSearchPlaceholder')}
              className="h-8 w-full rounded-lg border border-border bg-bg pl-8 pr-3 text-[12px] leading-[18px] text-text outline-none focus:border-border-hover"
              data-testid="agent-group-editor-skill-picker-search"
            />
          </div>
          <Tabs
            className="mb-4"
            items={[
              {
                value: 'market',
                label: t('agentManagement.form.skillMarket'),
                testId: 'agent-group-editor-skill-picker-tab-market',
              },
              {
                value: 'local',
                label: t('agentManagement.form.mySkills'),
                testId: 'agent-group-editor-skill-picker-tab-local',
              },
            ]}
            value={skillSourceTab}
            onChange={setSkillSourceTab}
            wrapperTestId="agent-group-editor-skill-picker-tabs"
            role="tablist"
            ariaLabel={t('agentManagement.group.picker.skillSourceTabsLabel')}
          />
          {selectionError ? (
            <div
              className="agent-management-form-error mb-4"
              role="alert"
              data-testid="agent-group-editor-selection-error"
            >
              {selectionError}
            </div>
          ) : null}
          <div className="flex-1 overflow-y-auto">
            {skillsStatus === 'loading' ? (
              <p className="py-10 text-center text-[13px] text-text-muted">{t('common.loading')}</p>
            ) : skillsStatus === 'error' ? (
              <div className="agent-management-form-error">
                <span>{t('agentManagement.group.form.skillsError')}</span>
                <button type="button" data-testid="agent-group-editor-skill-picker-retry" onClick={onReloadSkills}>
                  {t('common.retry')}
                </button>
              </div>
            ) : filteredSkills.length === 0 ? (
              <div className="py-10 text-center text-[13px] text-text-muted">
                <p>{t('agentManagement.group.form.skillsEmpty')}</p>
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-4" data-testid="agent-group-editor-skill-picker-list">
                {filteredSkills.map((skill) => {
                  const selected = skillDraft.includes(skill.id);
                  const installed = skill.installed === true;
                  const installing = installingSkillId === skill.id;
                  return (
                    <PageCard
                      key={skill.id}
                      testId="agent-group-editor-skill-picker-item"
                      variant={skill.id}
                      interactive={installed}
                      selected={selected}
                      disabled={!installed && !onInstallSkill}
                      onClick={installed ? () => toggleSkill(skill.id) : undefined}
                      avatar={{ name: skill.name }}
                      title={skill.name}
                      description={skill.description || t('agentManagement.unknownDescription')}
                      actionSlot={
                        !installed && onInstallSkill ? (
                          <button
                            type="button"
                            className="agent-management-inline-action"
                            data-testid="agent-group-editor-skill-picker-install"
                            data-variant={skill.id}
                            disabled={installing}
                            aria-busy={installing}
                            onClick={(event) => {
                              event.stopPropagation();
                              void onInstallSkill(skill);
                            }}
                          >
                            {installing
                              ? t('agentManagement.form.installingSkill')
                              : t('agentManagement.form.installSkill')}
                          </button>
                        ) : (
                          <span className="shrink-0" aria-hidden="true">
                            {selected ? (
                              <Check size={14} className="text-[color:var(--color-chat-accent)]" />
                            ) : (
                              <Plus size={14} className="text-text-muted" />
                            )}
                          </span>
                        )
                      }
                    />
                  );
                })}
              </div>
            )}
          </div>
          <div className="mt-4 flex items-center justify-between border-t border-border pt-4">
            <span className="text-[13px] text-text-muted">
              {t('agentManagement.group.picker.selectedCount', { count: skillDraft.length })}
            </span>
          </div>
        </FormDrawer>
      )}
    </>
  );
}

function Section({
  title,
  action,
  collapsible,
  defaultOpen,
  onToggle,
  children,
}: {
  title: string;
  action?: ReactNode;
  collapsible?: boolean;
  defaultOpen?: boolean;
  onToggle?: () => void;
  children: ReactNode;
}) {
  const isOpen = collapsible ? defaultOpen : true;
  return (
    <div className="mb-6">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          {collapsible && (
            <button
              type="button"
              className="shrink-0 text-text-muted hover:text-text"
              onClick={onToggle}
              aria-expanded={isOpen}
            >
              {isOpen ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
            </button>
          )}
          <h2 className="text-[14px] font-semibold leading-[22px] text-text">{title}</h2>
        </div>
        {isOpen && action}
      </div>
      {isOpen && children}
    </div>
  );
}
