import { useMemo, useState, type FormEvent, type ReactNode } from 'react';
import { ChevronDown, ChevronUp, Minus, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  dedupeAgentGroupOptions,
  getAgentAvatarUrl,
  resolveAgentGroupSelectionId,
  type AgentCatalogItem,
  type AgentGroupDraft,
  type RequestStatus,
  type SkillOption,
} from '../../features/agentManagement';
import { AGENT_DESCRIPTION_MAX_LENGTH, AGENT_NAME_MAX_LENGTH } from '../../features/agentManagement/limits';
import { AgentGroupMemberPicker } from './AgentGroupMemberPicker';
import { AgentTagPicker } from './AgentTagPicker';
import { DeleteCardIcon, SwitchCardIcon } from './cardActions';
import { SkillPickerDrawer } from './SkillPickerDrawer';
import { PageCard, Tabs, Input, Textarea, FieldError } from '../ui';
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
  const [teamConfigOpen, setTeamConfigOpen] = useState(true);
  const [skillsOpen, setSkillsOpen] = useState(true);
  const [promptsOpen, setPromptsOpen] = useState(true);
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
  const update = (patch: Partial<AgentGroupDraft>) => onChange({ ...draft, ...patch });

  const nextPromptKey = () => {
    promptKeySeedRef.current += 1;
    return `prompt-${promptKeySeedRef.current}`;
  };
  const promptKeyAt = (index: number) => {
    promptKeysRef.current[index] ||= nextPromptKey();
    return promptKeysRef.current[index];
  };
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
              <FieldError className="mt-1" testId="agent-group-editor-name-error">
                {touched && errors.name ? errors.name : null}
              </FieldError>
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
              <FieldError className="mt-1" testId="agent-group-editor-description-error">
                {touched && errors.description ? errors.description : null}
              </FieldError>
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
              <FieldError className="mt-1" testId="agent-group-editor-persona-error">
                {touched && errors.persona ? errors.persona : null}
              </FieldError>
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
                  <div className="card-grid-auto">
                    <PageCard
                      testId="agent-group-editor-leader-card"
                      avatar={{ name: selectedLeader.displayName, iconUrl: getAgentAvatarUrl(selectedLeader) }}
                      title={selectedLeader.displayName}
                      description={selectedLeader.description || t('agentManagement.unknownDescription')}
                      actionsHover
                      action={{
                        icon: <SwitchCardIcon />,
                        onClick: () => openMemberPicker('leader'),
                      }}
                    />
                  </div>
                ) : touched && errors.leader ? (
                  <FieldError testId="agent-group-editor-leader-error">{errors.leader}</FieldError>
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
                  <div className="card-grid-auto">
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
                          icon: <DeleteCardIcon />,
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
                  <FieldError testId="agent-group-editor-members-error">{errors.members}</FieldError>
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
                onClick={() => setSkillPickerOpen(true)}
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
              <div className="card-grid-auto">
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
                        icon: <DeleteCardIcon />,
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
        <SkillPickerDrawer
          title={t('agentManagement.group.form.chooseSkills')}
          testId="agent-group-editor-skill-picker"
          status={skillsStatus}
          skills={skillOptions}
          initialSelectedIds={draft.skillRefs}
          onClose={() => setSkillPickerOpen(false)}
          onConfirm={(ids) => {
            update({ skillRefs: ids });
            setSkillPickerOpen(false);
          }}
          onRetry={onReloadSkills}
          onInstallSkill={onInstallSkill}
          installingSkillId={installingSkillId}
          tabsAriaLabel={t('agentManagement.group.picker.skillSourceTabsLabel')}
          errorMessage={t('agentManagement.group.form.skillsError')}
          emptyMessage={t('agentManagement.group.form.skillsEmpty')}
        />
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
