import { createPortal } from 'react-dom';
import { useMemo, useRef, useState, type FormEvent } from 'react';
import { ArrowLeftRight, Check, ChevronDown, ChevronUp, Minus, Plus, Search, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  resolveAgentGroupSelectionId,
  type AgentCatalogItem,
  type AgentGroupDraft,
  type RequestStatus,
  type SkillOption,
} from '../../features/agentManagement';
import { AGENT_DESCRIPTION_MAX_LENGTH, AGENT_NAME_MAX_LENGTH } from '../../features/agentManagement/limits';
import { AgentGroupMemberPicker, AgentOptionAvatar } from './AgentGroupMemberPicker';
import { AgentTagPicker } from './AgentTagPicker';
import { useDialogFocusTrap } from './useDialogFocusTrap';
import UninstallIcon from '../../assets/agent-management/uninstall.svg?react';
import BackIcon from '../../assets/work-mode/arrow-left.svg?react';

type AgentGroupEditorProps = {
  draft: AgentGroupDraft;
  agentOptions: AgentCatalogItem[];
  agentsStatus: RequestStatus;
  skillOptions: SkillOption[];
  skillsStatus: RequestStatus;
  saving: boolean;
  error: string | null;
  onChange: (draft: AgentGroupDraft) => void;
  onReloadAgents: () => void;
  onReloadSkills: () => void;
  onCreateAgent?: () => void;
  onCancel: () => void;
  onSave: () => void;
};

export function AgentGroupEditor({
  draft,
  agentOptions,
  agentsStatus,
  skillOptions,
  skillsStatus,
  saving,
  error,
  onChange,
  onReloadAgents,
  onReloadSkills,
  onCreateAgent,
  onCancel,
  onSave,
}: AgentGroupEditorProps) {
  const { t } = useTranslation();
  const [touched, setTouched] = useState(false);
  const [pickerMode, setPickerMode] = useState<'leader' | 'member' | null>(null);
  const [skillPickerOpen, setSkillPickerOpen] = useState(false);
  const [skillQuery, setSkillQuery] = useState('');
  const [skillDraft, setSkillDraft] = useState<string[]>(draft.skillRefs);
  const [teamConfigOpen, setTeamConfigOpen] = useState(true);
  const [skillsOpen, setSkillsOpen] = useState(true);
  const [promptsOpen, setPromptsOpen] = useState(true);
  const leaderPickerTriggerRef = useRef<HTMLElement | null>(null);
  const memberPickerTriggerRef = useRef<HTMLElement | null>(null);
  const skillPickerTriggerRef = useRef<HTMLElement | null>(null);
  const skillDialogRef = useRef<HTMLElement>(null);
  const promptKeySeedRef = useRef(0);
  const promptKeysRef = useRef<string[]>([]);

  const errors = useMemo(
    () => ({
      name: !draft.name.trim() ? t('agentManagement.group.form.errors.nameRequired') : '',
      description: !draft.description.trim() ? t('agentManagement.group.form.errors.descriptionRequired') : '',
      persona: !draft.persona.trim() ? t('agentManagement.group.form.errors.personaRequired') : '',
      leader: !draft.leaderId ? t('agentManagement.group.form.errors.leaderRequired') : '',
    }),
    [draft.description, draft.leaderId, draft.name, draft.persona, t],
  );
  const hasErrors = Object.values(errors).some(Boolean);
  const selectedLeader = agentOptions.find(
    (agent) => resolveAgentGroupSelectionId(agent) === draft.leaderId || agent.id === draft.leaderId,
  );
  const selectedMembers = agentOptions.filter(
    (agent) => draft.memberIds.includes(resolveAgentGroupSelectionId(agent)) || draft.memberIds.includes(agent.id),
  );
  const filteredSkills = skillOptions.filter((skill) =>
    `${skill.id} ${skill.name} ${skill.description}`
      .toLocaleLowerCase()
      .includes(skillQuery.trim().toLocaleLowerCase()),
  );
  const update = (patch: Partial<AgentGroupDraft>) => onChange({ ...draft, ...patch });
  useDialogFocusTrap({
    dialogRef: skillDialogRef,
    restoreFocusRef: skillPickerTriggerRef,
    onEscape: () => setSkillPickerOpen(false),
    open: skillPickerOpen,
  });

  const nextPromptKey = () => {
    promptKeySeedRef.current += 1;
    return `prompt-${promptKeySeedRef.current}`;
  };
  const promptKeyAt = (index: number) => {
    promptKeysRef.current[index] ||= nextPromptKey();
    return promptKeysRef.current[index];
  };
  const openSkills = (trigger: HTMLElement) => {
    skillPickerTriggerRef.current = trigger;
    setSkillDraft(draft.skillRefs);
    setSkillQuery('');
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
  const openMemberPicker = (mode: 'leader' | 'member', trigger: HTMLElement) => {
    if (agentsStatus !== 'loading' && agentOptions.length === 0) onReloadAgents();
    (mode === 'leader' ? leaderPickerTriggerRef : memberPickerTriggerRef).current = trigger;
    setPickerMode(mode);
  };
  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (!hasErrors) onSave();
  };

  return (
    <form
      className="agent-management-editor agent-group-editor"
      onSubmit={handleSubmit}
      data-testid="agent-group-editor"
    >
      <button type="button" className="detail-back" data-testid="agent-group-editor-back" onClick={onCancel}>
        <BackIcon aria-hidden="true" />
        {t('agentManagement.actions.back')}
      </button>
      <div className="detail-body flex-1 min-h-0 overflow-y-auto">
        <div className="agent-management-editor__inner">
          <header className="agent-management-editor__header agent-group-editor__header">
            <h1>{t('agentManagement.group.form.title')}</h1>
            <div
              className="agent-management-editor__tabs"
              role="tablist"
              aria-label={t('agentManagement.form.createTabsLabel')}
            >
              <button
                type="button"
                role="tab"
                aria-selected="false"
                data-testid="agent-group-editor-agent-tab"
                onClick={onCreateAgent}
              >
                {t('agentManagement.group.form.createAgentTab')}
              </button>
              <span role="tab" aria-selected="true" className="is-active" data-testid="agent-group-editor-group-tab">
                {t('agentManagement.group.form.createGroupTab')}
              </span>
            </div>
          </header>

          {error ? (
            <div className="agent-management-form-error" role="alert">
              {error}
            </div>
          ) : null}

          <section className="agent-management-form-section">
            <h2>{t('agentManagement.group.form.basic')}</h2>
            <div className="agent-management-form-grid">
              <div className="agent-management-form-field--wide">
                <div className="agent-management-form-field__label-row">
                  <label htmlFor="agent-management-group-name">{t('agentManagement.group.form.nameLabel')}</label>
                  <span
                    aria-hidden="true"
                    className={`agent-management-field-counter${draft.name.length >= AGENT_NAME_MAX_LENGTH ? ' is-limit' : ''}`}
                    data-testid="agent-group-editor-name-counter"
                  >
                    {t('agentManagement.form.charCount', { count: draft.name.length, max: AGENT_NAME_MAX_LENGTH })}
                  </span>
                </div>
                <input
                  id="agent-management-group-name"
                  data-testid="agent-group-editor-name"
                  value={draft.name}
                  onChange={(event) => update({ name: event.target.value })}
                  placeholder={t('agentManagement.group.form.namePlaceholder')}
                  maxLength={AGENT_NAME_MAX_LENGTH}
                  aria-invalid={Boolean(touched && errors.name)}
                />
                {touched && errors.name ? <small className="agent-management-field-error">{errors.name}</small> : null}
              </div>
              <div className="agent-management-form-field--wide">
                <div className="agent-management-form-field__label-row">
                  <label htmlFor="agent-management-group-description">
                    {t('agentManagement.group.form.descriptionLabel')}
                  </label>
                  <span
                    aria-hidden="true"
                    className={`agent-management-field-counter${draft.description.length >= AGENT_DESCRIPTION_MAX_LENGTH ? ' is-limit' : ''}`}
                    data-testid="agent-group-editor-description-counter"
                  >
                    {t('agentManagement.form.charCount', {
                      count: draft.description.length,
                      max: AGENT_DESCRIPTION_MAX_LENGTH,
                    })}
                  </span>
                </div>
                <textarea
                  id="agent-management-group-description"
                  data-testid="agent-group-editor-description"
                  rows={2}
                  value={draft.description}
                  onChange={(event) => update({ description: event.target.value })}
                  placeholder={t('agentManagement.group.form.descriptionPlaceholder')}
                  maxLength={AGENT_DESCRIPTION_MAX_LENGTH}
                  aria-invalid={Boolean(touched && errors.description)}
                />
                {touched && errors.description ? (
                  <small className="agent-management-field-error">{errors.description}</small>
                ) : null}
              </div>
              <div className="agent-management-form-field--wide agent-management-form-field--tag-picker">
                <span>{t('agentManagement.group.form.tagLabel')}</span>
                <AgentTagPicker
                  tagIds={draft.tagIds}
                  customTags={draft.customTags}
                  label={t('agentManagement.group.form.tagLabel')}
                  placeholder={t('agentManagement.group.form.tagPlaceholder')}
                  onChange={(value) => update(value)}
                />
              </div>
            </div>
          </section>

          <section className="agent-management-form-section agent-group-editor__intro-section">
            <h2>{t('agentManagement.group.form.teamIntro')}</h2>
            <div className="agent-management-form-field--wide agent-group-editor__intro-field">
              <textarea
                data-testid="agent-group-editor-persona"
                rows={8}
                value={draft.persona}
                onChange={(event) => update({ persona: event.target.value })}
                placeholder={t('agentManagement.group.form.personaPlaceholder')}
                aria-invalid={Boolean(touched && errors.persona)}
                aria-label={t('agentManagement.group.form.personaLabel')}
              />
              {touched && errors.persona ? (
                <small className="agent-management-field-error">{errors.persona}</small>
              ) : null}
            </div>
          </section>

          <section className="agent-management-form-section agent-group-editor__team-config-section">
            <div className="agent-management-form-section__header">
              <div className="agent-management-form-section__heading">
                <button
                  type="button"
                  className="agent-management-section-toggle"
                  data-testid="agent-group-editor-team-config-toggle"
                  aria-expanded={teamConfigOpen}
                  aria-label={t('agentManagement.group.form.teamConfig')}
                  onClick={() => setTeamConfigOpen((open) => !open)}
                >
                  {teamConfigOpen ? (
                    <ChevronUp size={18} aria-hidden="true" />
                  ) : (
                    <ChevronDown size={18} aria-hidden="true" />
                  )}
                </button>
                <h2>{t('agentManagement.group.form.teamConfig')}</h2>
              </div>
            </div>
            {teamConfigOpen ? (
              <div className="agent-group-editor__selection-grid">
                <div className="agent-group-editor__selection-field agent-group-editor__leader-field">
                  <div className="agent-group-editor__selection-label">
                    <span>{t('agentManagement.group.form.leaderFieldLabel')}</span>
                    {!selectedLeader ? (
                      <button
                        type="button"
                        className="agent-management-inline-action"
                        data-testid="agent-group-editor-add-leader"
                        onClick={(event) => openMemberPicker('leader', event.currentTarget)}
                      >
                        <Plus size={14} aria-hidden="true" />
                        {t('agentManagement.group.form.leaderLabel')}
                      </button>
                    ) : null}
                  </div>
                  {selectedLeader ? (
                    <article className="agent-group-editor__selected-agent">
                      <AgentOptionAvatar agent={selectedLeader} />
                      <span className="agent-group-editor__selected-agent-copy">
                        <strong title={selectedLeader.displayName}>{selectedLeader.displayName}</strong>
                        <p>{selectedLeader.description || t('agentManagement.unknownDescription')}</p>
                      </span>
                      <button
                        type="button"
                        className="agent-group-editor__selected-agent-action"
                        data-testid="agent-group-editor-change-leader"
                        aria-label={t('agentManagement.group.form.changeLeader')}
                        onClick={(event) => openMemberPicker('leader', event.currentTarget)}
                      >
                        <ArrowLeftRight size={18} aria-hidden="true" />
                      </button>
                    </article>
                  ) : touched && errors.leader ? (
                    <small className="agent-management-field-error">{errors.leader}</small>
                  ) : null}
                </div>
                <div className="agent-group-editor__selection-field agent-group-editor__members-field">
                  <div className="agent-group-editor__selection-label">
                    <span>{t('agentManagement.group.form.membersFieldLabel')}</span>
                    <button
                      type="button"
                      className="agent-management-inline-action"
                      data-testid="agent-group-editor-add-member"
                      onClick={(event) => openMemberPicker('member', event.currentTarget)}
                    >
                      <Plus size={14} aria-hidden="true" />
                      {t('agentManagement.group.form.addMember')}
                    </button>
                  </div>
                  {selectedMembers.length > 0 ? (
                    <div className="agent-group-editor__member-cards">
                      {selectedMembers.map((agent) => (
                        <article key={agent.id} className="agent-group-editor__member-card">
                          <div className="agent-group-editor__member-card-top">
                            <AgentOptionAvatar agent={agent} />
                            <span className="agent-group-editor__member-card-copy">
                              <strong title={agent.displayName}>{agent.displayName}</strong>
                              <p>{agent.description || t('agentManagement.unknownDescription')}</p>
                            </span>
                            <button
                              type="button"
                              className="agent-management-capability-card__remove"
                              data-testid="agent-group-editor-remove-member"
                              data-variant={resolveAgentGroupSelectionId(agent)}
                              aria-label={t('agentManagement.group.form.removeMember', { name: agent.displayName })}
                              onClick={() =>
                                update({
                                  memberIds: draft.memberIds.filter(
                                    (id) => id !== resolveAgentGroupSelectionId(agent) && id !== agent.id,
                                  ),
                                })
                              }
                            >
                              <UninstallIcon aria-hidden="true" />
                            </button>
                          </div>
                        </article>
                      ))}
                    </div>
                  ) : null}
                </div>
              </div>
            ) : null}
          </section>

          <section className="agent-management-form-section agent-group-editor__skills-section">
            <div className="agent-management-form-section__header">
              <div className="agent-management-form-section__heading">
                <button
                  type="button"
                  className="agent-management-section-toggle"
                  data-testid="agent-group-editor-skills-toggle"
                  aria-expanded={skillsOpen}
                  aria-label={t('agentManagement.group.form.skillsLabel')}
                  onClick={() => setSkillsOpen((open) => !open)}
                >
                  {skillsOpen ? (
                    <ChevronUp size={18} aria-hidden="true" />
                  ) : (
                    <ChevronDown size={18} aria-hidden="true" />
                  )}
                </button>
                <h2>{t('agentManagement.group.form.skillsLabel')}</h2>
              </div>
              <button
                type="button"
                className="agent-management-inline-action"
                data-testid="agent-group-editor-choose-skills"
                onClick={(event) => openSkills(event.currentTarget)}
              >
                <Plus size={14} aria-hidden="true" />
                {t('agentManagement.group.form.chooseSkills')}
              </button>
            </div>
            {skillsOpen && draft.skillRefs.length > 0 ? (
              <div className="agent-management-selected-capabilities">
                {draft.skillRefs.map((id) => {
                  const skill = skillOptions.find((option) => option.id === id);
                  const name = skill?.name || id;
                  return (
                    <article key={id} className="agent-management-capability-card">
                      <div className="agent-management-capability-card__heading">
                        <span className="agent-management-capability-card__icon">{name.slice(0, 1).toUpperCase()}</span>
                        <strong title={name}>{name}</strong>
                        <button
                          type="button"
                          className="agent-management-capability-card__remove"
                          data-testid="agent-group-editor-remove-skill"
                          data-variant={id}
                          aria-label={t('agentManagement.group.form.removeSkill', { name })}
                          onClick={() => update({ skillRefs: draft.skillRefs.filter((item) => item !== id) })}
                        >
                          <UninstallIcon aria-hidden="true" />
                        </button>
                      </div>
                      <small>{skill?.description || t('agentManagement.unknownDescription')}</small>
                    </article>
                  );
                })}
              </div>
            ) : null}
          </section>

          <section className="agent-management-form-section agent-management-form-section--prompts">
            <div className="agent-management-form-section__header">
              <div className="agent-management-form-section__heading">
                <button
                  type="button"
                  className="agent-management-section-toggle"
                  data-testid="agent-group-editor-prompts-toggle"
                  aria-expanded={promptsOpen}
                  aria-label={t('agentManagement.group.form.promptsLabel')}
                  onClick={() => setPromptsOpen((open) => !open)}
                >
                  {promptsOpen ? (
                    <ChevronUp size={18} aria-hidden="true" />
                  ) : (
                    <ChevronDown size={18} aria-hidden="true" />
                  )}
                </button>
                <h2>{t('agentManagement.group.form.promptsLabel')}</h2>
              </div>
              <button
                type="button"
                className="agent-management-inline-action"
                data-testid="agent-group-editor-add-prompt"
                onClick={addPrompt}
              >
                <Plus size={14} aria-hidden="true" />
                {t('agentManagement.group.form.addPrompt')}
              </button>
            </div>
            {promptsOpen ? (
              <div className="agent-management-prompt-editor-list">
                {draft.suggestedPrompts.map((prompt, index) => (
                  <div key={promptKeyAt(index)} className="agent-management-prompt-editor">
                    <input
                      data-testid="agent-group-editor-prompt-input"
                      data-variant={promptKeyAt(index)}
                      value={prompt}
                      onChange={(event) => updatePrompt(index, event.target.value)}
                      placeholder={t('agentManagement.group.form.promptPlaceholder')}
                    />
                    <button
                      type="button"
                      data-testid="agent-group-editor-remove-prompt"
                      data-variant={promptKeyAt(index)}
                      aria-label={t('agentManagement.group.form.removePrompt')}
                      onClick={() => removePrompt(index)}
                    >
                      <Minus size={16} aria-hidden="true" />
                    </button>
                  </div>
                ))}
              </div>
            ) : null}
          </section>

          <footer className="agent-management-editor__footer">
            <button
              type="button"
              className="agent-management-button agent-management-button--secondary"
              data-testid="agent-group-editor-cancel"
              onClick={onCancel}
            >
              {t('common.cancel')}
            </button>
            <button
              type="submit"
              className="agent-management-button agent-management-button--primary"
              data-testid="agent-group-editor-save"
              disabled={saving}
            >
              {saving ? t('agentManagement.group.actions.saving') : t('common.confirm')}
            </button>
          </footer>
        </div>
      </div>

      {pickerMode ? (
        <AgentGroupMemberPicker
          mode={pickerMode}
          agents={agentOptions}
          selectedLeaderId={draft.leaderId}
          selectedMemberIds={draft.memberIds}
          restoreFocusRef={pickerMode === 'leader' ? leaderPickerTriggerRef : memberPickerTriggerRef}
          onCancel={() => setPickerMode(null)}
          onConfirm={(ids) => {
            if (pickerMode === 'leader')
              update({ leaderId: ids[0] || '', memberIds: draft.memberIds.filter((id) => id !== ids[0]) });
            else update({ memberIds: ids.filter((id) => id !== draft.leaderId) });
            setPickerMode(null);
          }}
        />
      ) : null}
      {skillPickerOpen
        ? createPortal(
            <div
              className="agent-management-selection-overlay"
              role="presentation"
              onMouseDown={(event) => {
                if (event.target === event.currentTarget) setSkillPickerOpen(false);
              }}
            >
              <section
                ref={skillDialogRef}
                className="agent-management-selection-dialog"
                data-testid="agent-group-editor-skill-picker"
                role="dialog"
                aria-modal="true"
                aria-labelledby="agent-group-skill-picker-title"
                tabIndex={-1}
                onMouseDown={(event) => event.stopPropagation()}
              >
                <header>
                  <h2 id="agent-group-skill-picker-title">{t('agentManagement.group.form.chooseSkills')}</h2>
                  <button
                    type="button"
                    aria-label={t('common.close')}
                    data-testid="agent-group-editor-skill-picker-close"
                    onClick={() => setSkillPickerOpen(false)}
                  >
                    <X size={18} aria-hidden="true" />
                  </button>
                </header>
                <label className="agent-management-selection-search">
                  <Search size={16} aria-hidden="true" />
                  <input
                    type="search"
                    data-testid="agent-group-editor-skill-picker-search"
                    value={skillQuery}
                    onChange={(event) => setSkillQuery(event.target.value)}
                    placeholder={t('agentManagement.form.selectionSearchPlaceholder')}
                    autoFocus
                  />
                </label>
                <div
                  className={`agent-management-selection-dialog__body${skillsStatus === 'success' && filteredSkills.length === 0 ? ' is-empty' : ''}`}
                >
                  {skillsStatus === 'loading' ? (
                    <p className="agent-management-form-muted">{t('common.loading')}</p>
                  ) : null}
                  {skillsStatus === 'error' ? (
                    <div className="agent-management-form-error">
                      <span>{t('agentManagement.group.form.skillsError')}</span>
                      <button
                        type="button"
                        data-testid="agent-group-editor-skill-picker-retry"
                        onClick={onReloadSkills}
                      >
                        {t('common.retry')}
                      </button>
                    </div>
                  ) : null}
                  {skillsStatus === 'success' && filteredSkills.length === 0 ? (
                    <div className="agent-management-selection-empty-state">
                      <p>{t('agentManagement.group.form.skillsEmpty')}</p>
                    </div>
                  ) : null}
                  {skillsStatus === 'success' && filteredSkills.length > 0 ? (
                    <div className="agent-management-selection-grid">
                      {filteredSkills.map((skill) => {
                        const selected = skillDraft.includes(skill.id);
                        return (
                          <button
                            key={skill.id}
                            type="button"
                            className={`agent-management-selection-card${selected ? ' is-selected' : ''}`}
                            data-testid="agent-group-editor-skill-picker-item"
                            data-variant={skill.id}
                            onClick={() => toggleSkill(skill.id)}
                            aria-pressed={selected}
                          >
                            <span className="agent-management-capability-card__icon">
                              {skill.name.slice(0, 1).toUpperCase()}
                            </span>
                            <span>
                              <strong title={skill.name}>{skill.name}</strong>
                              <small>{skill.description || t('agentManagement.unknownDescription')}</small>
                            </span>
                            <span className="agent-management-selection-card__action" aria-hidden="true">
                              {selected ? <Check size={12} strokeWidth={2.5} /> : null}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  ) : null}
                </div>
                <footer>
                  <span>{t('agentManagement.group.picker.selectedCount', { count: skillDraft.length })}</span>
                  <div>
                    <button
                      type="button"
                      className="agent-management-button agent-management-button--secondary"
                      data-testid="agent-group-editor-skill-picker-cancel"
                      onClick={() => setSkillPickerOpen(false)}
                    >
                      {t('common.cancel')}
                    </button>
                    <button
                      type="button"
                      className="agent-management-button agent-management-button--primary"
                      data-testid="agent-group-editor-skill-picker-confirm"
                      onClick={() => {
                        update({ skillRefs: skillDraft });
                        setSkillPickerOpen(false);
                      }}
                    >
                      {t('common.confirm')}
                    </button>
                  </div>
                </footer>
              </section>
            </div>,
            document.body,
          )
        : null}
    </form>
  );
}
