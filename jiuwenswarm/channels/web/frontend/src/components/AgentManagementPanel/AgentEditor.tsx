import { Check, ChevronDown, ChevronUp, Minus, X } from 'lucide-react';
import { createPortal } from 'react-dom';
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import PlusIcon from '../../assets/agent-management/agent-plus.svg?react';
import SearchIcon from '../../assets/agent-management/agent-search.svg?react';
import UninstallIcon from '../../assets/agent-management/uninstall.svg?react';
import BackIcon from '../../assets/work-mode/arrow-left.svg?react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  isTeamSkillOption,
  isSkillVisibleInSourceTab,
  isMcpSelectable,
  sortInstalledFirst,
  sortMcpOptions,
  type AgentDraft,
  type McpOption,
  type RequestStatus,
  type SkillOption,
} from '../../features/agentManagement';
import { AGENT_DESCRIPTION_MAX_LENGTH, AGENT_NAME_MAX_LENGTH } from '../../features/agentManagement/limits';
import { AgentTagPicker } from './AgentTagPicker';
import { PageCard, Tabs } from '../ui';
import { SelectionPagination, useSelectionPagination } from './SelectionPagination';

type AgentEditorProps = {
  draft: AgentDraft;
  mode?: 'create' | 'edit';
  skillOptions: SkillOption[];
  skillsStatus: RequestStatus;
  mcpOptions: McpOption[];
  mcpStatus: RequestStatus;
  saving: boolean;
  error: string | null;
  selectionError?: string | null;
  onChange: (draft: AgentDraft) => void;
  onReloadSkills: () => void;
  onReloadMcps: () => void;
  onInstallSkill?: (skill: SkillOption) => void | Promise<void>;
  installingSkillId?: string | null;
  onConnectMcp?: (mcp: McpOption) => void;
  connectingMcpId?: string | null;
  onInstallMcp?: (mcp: McpOption) => void | Promise<void>;
  installingMcpId?: string | null;
  onCreateGroup?: () => void;
  onCancel: () => void;
  onSave: () => void;
};

export function AgentEditor({
  draft,
  mode = 'create',
  skillOptions,
  skillsStatus,
  mcpOptions,
  mcpStatus,
  saving,
  error,
  selectionError,
  onChange,
  onReloadSkills,
  onReloadMcps,
  onInstallSkill,
  installingSkillId,
  onConnectMcp,
  connectingMcpId,
  onInstallMcp,
  installingMcpId,
  onCreateGroup,
  onCancel,
  onSave,
}: AgentEditorProps) {
  const { t } = useTranslation();
  const [touched, setTouched] = useState(false);
  const [mcpOpen, setMcpOpen] = useState(true);
  const [skillsOpen, setSkillsOpen] = useState(true);
  const [promptsOpen, setPromptsOpen] = useState(true);
  const [personaEditing, setPersonaEditing] = useState(false);
  const [skillDialogOpen, setSkillDialogOpen] = useState(false);
  const [mcpDialogOpen, setMcpDialogOpen] = useState(false);
  const [skillQuery, setSkillQuery] = useState('');
  const [mcpQuery, setMcpQuery] = useState('');
  const [skillSourceTab, setSkillSourceTab] = useState<'local' | 'market'>('market');
  const [mcpSourceTab, setMcpSourceTab] = useState<'market' | 'installed'>('market');
  const [skillDraft, setSkillDraft] = useState<string[]>(draft.skillRefs);
  const [mcpDraft, setMcpDraft] = useState<string[]>(draft.mcpRefs);
  const personaSurfaceRef = useRef<HTMLDivElement>(null);
  const skillDialogRef = useRef<HTMLElement>(null);
  const mcpDialogRef = useRef<HTMLElement>(null);
  const skillDialogTriggerRef = useRef<HTMLButtonElement>(null);
  const mcpDialogTriggerRef = useRef<HTMLButtonElement>(null);

  const errors = useMemo(
    () => ({
      name: !draft.name.trim() ? t('agentManagement.form.errors.nameRequired') : '',
      description: !draft.description.trim() ? t('agentManagement.form.errors.descriptionRequired') : '',
      persona: !draft.persona.trim() ? t('agentManagement.form.errors.personaRequired') : '',
    }),
    [draft.description, draft.name, draft.persona, t],
  );
  const hasErrors = Object.values(errors).some(Boolean);
  const selectedSkills = skillOptions.filter((skill) => draft.skillRefs.includes(skill.id));
  const selectedMcps = mcpOptions.filter((mcp) => draft.mcpRefs.includes(mcp.id));
  const filteredSkills = sortInstalledFirst(
    skillOptions.filter((skill) => {
      if (isTeamSkillOption(skill, skillSourceTab) || !isSkillVisibleInSourceTab(skill, skillSourceTab)) return false;
      return `${skill.id} ${skill.name} ${skill.description}`
        .toLocaleLowerCase()
        .includes(skillQuery.trim().toLocaleLowerCase());
    }),
  );
  const filteredMcps = sortMcpOptions(
    mcpOptions.filter((mcp) => {
      const isMarketplace = mcp.source === 'built_in' || mcp.source === 'hub';
      const isMine = mcp.installed === true || mcp.source === 'customize';
      if (mcpSourceTab === 'market' ? !isMarketplace : !isMine) return false;
      return `${mcp.id} ${mcp.name} ${mcp.description}`
        .toLocaleLowerCase()
        .includes(mcpQuery.trim().toLocaleLowerCase());
    }),
  );
  const {
    pageItems: pageSkills,
    page: skillPage,
    totalPages: skillTotalPages,
    setPage: setSkillPage,
  } = useSelectionPagination(filteredSkills, `${skillSourceTab}\0${skillQuery}`);
  const {
    pageItems: pageMcps,
    page: mcpPage,
    totalPages: mcpTotalPages,
    setPage: setMcpPage,
  } = useSelectionPagination(filteredMcps, `${mcpSourceTab}\0${mcpQuery}`);

  useEffect(() => {
    if (!personaEditing) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (!personaSurfaceRef.current?.contains(event.target as Node)) setPersonaEditing(false);
    };
    document.addEventListener('pointerdown', handlePointerDown, true);
    return () => document.removeEventListener('pointerdown', handlePointerDown, true);
  }, [personaEditing]);

  useEffect(() => {
    if (!skillDialogOpen && !mcpDialogOpen) return;
    const dialog = skillDialogOpen ? skillDialogRef.current : mcpDialogRef.current;
    const restoreTarget = skillDialogOpen ? skillDialogTriggerRef.current : mcpDialogTriggerRef.current;
    if (!dialog) return;
    const focusableSelector = 'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])';
    const focusFirst = () => dialog.querySelector<HTMLElement>(focusableSelector)?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        if (skillDialogOpen) setSkillDialogOpen(false);
        else setMcpDialogOpen(false);
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(focusableSelector));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    const focusTimer = window.setTimeout(focusFirst, 0);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      window.clearTimeout(focusTimer);
      restoreTarget?.focus();
    };
  }, [mcpDialogOpen, skillDialogOpen]);

  const update = (patch: Partial<AgentDraft>) => onChange({ ...draft, ...patch });

  const openSkillDialog = () => {
    setSkillDraft(draft.skillRefs);
    setSkillQuery('');
    setSkillSourceTab('market');
    setSkillDialogOpen(true);
  };

  const openMcpDialog = () => {
    setMcpDraft(draft.mcpRefs);
    setMcpQuery('');
    setMcpSourceTab('market');
    setMcpDialogOpen(true);
  };

  const updatePrompt = (index: number, value: string) => {
    const suggestedPrompts = draft.suggestedPrompts.map((prompt, promptIndex) =>
      promptIndex === index ? value : prompt,
    );
    update({ suggestedPrompts });
  };

  const addPrompt = () => {
    if (draft.suggestedPrompts.some((prompt) => prompt.trim().length === 0)) return;
    update({ suggestedPrompts: [...draft.suggestedPrompts, ''] });
  };

  const removePrompt = (index: number) =>
    update({ suggestedPrompts: draft.suggestedPrompts.filter((_, promptIndex) => promptIndex !== index) });

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (!hasErrors) onSave();
  };

  return (
    <form className="agent-management-editor" onSubmit={handleSubmit} data-testid="agent-editor">
      <button type="button" className="detail-back" onClick={onCancel}>
        <BackIcon aria-hidden="true" />
        {t('agentManagement.actions.back')}
      </button>

      <div className="detail-body flex-1 min-h-0 overflow-y-auto">
        <div className="agent-management-editor__inner">
          <header className="agent-management-editor__header">
            <h1>{mode === 'edit' ? t('agentManagement.form.editTitle') : t('agentManagement.form.title')}</h1>
            <div
              className="agent-management-editor__tabs"
              role="tablist"
              aria-label={t('agentManagement.form.createTabsLabel')}
            >
              <span className="is-active" role="tab" aria-selected="true">
                {t('agentManagement.form.createAgentTab')}
              </span>
              <button
                type="button"
                role="tab"
                aria-selected="false"
                data-testid="agent-editor-group-tab"
                onClick={onCreateGroup}
              >
                {t('agentManagement.group.form.createGroupTab')}
              </button>
            </div>
          </header>

          <section className="agent-management-form-section">
            <h2>{t('agentManagement.form.basic')}</h2>
            <div className="agent-management-form-grid">
              <div className="agent-management-form-field--wide">
                <div className="agent-management-form-field__label-row">
                  <label htmlFor="agent-management-agent-name">{t('agentManagement.form.nameLabel')}</label>
                  <span
                    aria-hidden="true"
                    className={`agent-management-field-counter${draft.name.length >= AGENT_NAME_MAX_LENGTH ? ' is-limit' : ''}`}
                    data-testid="agent-editor-name-counter"
                  >
                    {t('agentManagement.form.charCount', { count: draft.name.length, max: AGENT_NAME_MAX_LENGTH })}
                  </span>
                </div>
                <input
                  id="agent-management-agent-name"
                  value={draft.name}
                  onChange={(event) => update({ name: event.target.value })}
                  placeholder={t('agentManagement.form.namePlaceholder')}
                  maxLength={AGENT_NAME_MAX_LENGTH}
                  aria-invalid={Boolean(touched && errors.name)}
                />
                {touched && errors.name ? <small className="agent-management-field-error">{errors.name}</small> : null}
              </div>
              <div className="agent-management-form-field--wide">
                <div className="agent-management-form-field__label-row">
                  <label htmlFor="agent-management-agent-description">
                    {t('agentManagement.form.descriptionLabel')}
                  </label>
                  <span
                    aria-hidden="true"
                    className={`agent-management-field-counter${draft.description.length >= AGENT_DESCRIPTION_MAX_LENGTH ? ' is-limit' : ''}`}
                    data-testid="agent-editor-description-counter"
                  >
                    {t('agentManagement.form.charCount', {
                      count: draft.description.length,
                      max: AGENT_DESCRIPTION_MAX_LENGTH,
                    })}
                  </span>
                </div>
                <textarea
                  id="agent-management-agent-description"
                  rows={2}
                  value={draft.description}
                  onChange={(event) => update({ description: event.target.value })}
                  placeholder={t('agentManagement.form.descriptionPlaceholder')}
                  maxLength={AGENT_DESCRIPTION_MAX_LENGTH}
                  aria-invalid={Boolean(touched && errors.description)}
                />
                {touched && errors.description ? (
                  <small className="agent-management-field-error">{errors.description}</small>
                ) : null}
              </div>
              <div className="agent-management-form-field--wide agent-management-form-field--tag-picker">
                <span>{t('agentManagement.form.tagLabel')}</span>
                <AgentTagPicker
                  tagIds={draft.tagIds}
                  customTags={draft.customTags}
                  label={t('agentManagement.form.tagLabel')}
                  placeholder={t('agentManagement.form.tagPlaceholder')}
                  onChange={(value) => update(value)}
                />
              </div>
              <div className="agent-management-form-field--wide agent-management-persona-field">
                <span>{t('agentManagement.form.personaLabel')}</span>
                <div className="agent-management-persona-surface" ref={personaSurfaceRef}>
                  {personaEditing ? (
                    <textarea
                      className="agent-management-persona"
                      rows={12}
                      value={draft.persona}
                      onChange={(event) => update({ persona: event.target.value })}
                      placeholder={t('agentManagement.form.personaPlaceholder')}
                      aria-invalid={Boolean(touched && errors.persona)}
                      aria-label={t('agentManagement.form.personaLabel')}
                      onBlur={() => setPersonaEditing(false)}
                    />
                  ) : (
                    <div
                      className="agent-management-persona-rendered"
                      role="button"
                      tabIndex={0}
                      aria-label={t('agentManagement.form.personaPreview')}
                      onClick={() => setPersonaEditing(true)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault();
                          setPersonaEditing(true);
                        }
                      }}
                    >
                      {draft.persona.trim() ? (
                        <div className="agent-management-markdown">
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>{draft.persona}</ReactMarkdown>
                        </div>
                      ) : (
                        <span className="agent-management-persona-placeholder">
                          {t('agentManagement.form.personaPlaceholder')}
                        </span>
                      )}
                    </div>
                  )}
                </div>
                {touched && errors.persona ? (
                  <small className="agent-management-field-error">{errors.persona}</small>
                ) : null}
              </div>
            </div>
          </section>

          <section className="agent-management-form-section agent-management-form-section--mcp">
            <div className="agent-management-form-section__header">
              <div className="agent-management-form-section__heading">
                <button
                  type="button"
                  className="agent-management-section-toggle"
                  aria-expanded={mcpOpen}
                  aria-label={t('agentManagement.form.mcpToggle')}
                  onClick={() => setMcpOpen((open) => !open)}
                >
                  {mcpOpen ? <ChevronUp size={18} aria-hidden="true" /> : <ChevronDown size={18} aria-hidden="true" />}
                </button>
                <div>
                  <h2>{t('agentManagement.form.mcpLabel')}</h2>
                </div>
              </div>
              <button
                ref={mcpDialogTriggerRef}
                type="button"
                className="agent-management-inline-action"
                onClick={openMcpDialog}
              >
                <PlusIcon aria-hidden="true" />
                {t('agentManagement.form.addMcp')}
              </button>
            </div>
            {mcpOpen ? (
              selectedMcps.length > 0 ? (
                <div className="agent-management-selected-capabilities">
                  {selectedMcps.map((mcp) => (
                    <article className="agent-management-capability-card" key={mcp.id}>
                      <div className="agent-management-capability-card__heading">
                        <span className="agent-management-capability-card__icon">
                          {mcp.name.slice(0, 1).toUpperCase()}
                        </span>
                        <strong>{mcp.name}</strong>
                        <button
                          type="button"
                          className="agent-management-capability-card__remove"
                          aria-label={t('agentManagement.form.removeMcp', { name: mcp.name })}
                          onClick={() => update({ mcpRefs: draft.mcpRefs.filter((id) => id !== mcp.id) })}
                        >
                          <UninstallIcon aria-hidden="true" />
                        </button>
                      </div>
                      <small>{mcp.description}</small>
                    </article>
                  ))}
                </div>
              ) : null
            ) : null}
          </section>

          <section className="agent-management-form-section agent-management-form-section--skills">
            <div className="agent-management-form-section__header">
              <div className="agent-management-form-section__heading">
                <button
                  type="button"
                  className="agent-management-section-toggle"
                  aria-expanded={skillsOpen}
                  aria-label={t('agentManagement.form.skillsToggle')}
                  onClick={() => setSkillsOpen((open) => !open)}
                >
                  {skillsOpen ? (
                    <ChevronUp size={18} aria-hidden="true" />
                  ) : (
                    <ChevronDown size={18} aria-hidden="true" />
                  )}
                </button>
                <div>
                  <h2>{t('agentManagement.form.skillsLabel')}</h2>
                </div>
              </div>
              <button
                ref={skillDialogTriggerRef}
                type="button"
                className="agent-management-inline-action"
                onClick={openSkillDialog}
              >
                <PlusIcon aria-hidden="true" />
                {t('agentManagement.form.addSkill')}
              </button>
            </div>
            {skillsOpen ? (
              <>
                {skillsStatus === 'loading' ? (
                  <p className="agent-management-form-muted">{t('common.loading')}</p>
                ) : null}
                {skillsStatus === 'error' ? (
                  <div className="agent-management-form-error">
                    <span>{t('agentManagement.form.skillsError')}</span>
                    <button type="button" onClick={onReloadSkills}>
                      {t('common.retry')}
                    </button>
                  </div>
                ) : null}
                {selectedSkills.length > 0 ? (
                  <div className="agent-management-selected-capabilities">
                    {selectedSkills.map((skill) => (
                      <article className="agent-management-capability-card" key={skill.id}>
                        <div className="agent-management-capability-card__heading">
                          <span className="agent-management-capability-card__icon">
                            {skill.name.slice(0, 1).toUpperCase()}
                          </span>
                          <strong>{skill.name}</strong>
                          <button
                            type="button"
                            className="agent-management-capability-card__remove"
                            aria-label={t('agentManagement.form.removeSkill', { name: skill.name })}
                            onClick={() => update({ skillRefs: draft.skillRefs.filter((id) => id !== skill.id) })}
                          >
                            <UninstallIcon aria-hidden="true" />
                          </button>
                        </div>
                        <small>{skill.description}</small>
                      </article>
                    ))}
                  </div>
                ) : null}
              </>
            ) : null}
          </section>

          <section className="agent-management-form-section agent-management-form-section--prompts">
            <div className="agent-management-form-section__header">
              <div className="agent-management-form-section__heading">
                <button
                  type="button"
                  className="agent-management-section-toggle"
                  aria-expanded={promptsOpen}
                  aria-label={t('agentManagement.form.promptsToggle')}
                  onClick={() => setPromptsOpen((open) => !open)}
                >
                  {promptsOpen ? (
                    <ChevronUp size={18} aria-hidden="true" />
                  ) : (
                    <ChevronDown size={18} aria-hidden="true" />
                  )}
                </button>
                <h2>{t('agentManagement.form.promptsLabel')}</h2>
              </div>
              <button type="button" className="agent-management-inline-action" onClick={addPrompt}>
                <PlusIcon aria-hidden="true" />
                {t('agentManagement.form.addPrompt')}
              </button>
            </div>
            {promptsOpen ? (
              draft.suggestedPrompts.length > 0 ? (
                <div className="agent-management-prompt-editor-list">
                  {draft.suggestedPrompts.map((prompt, index) => (
                    <div className="agent-management-prompt-editor" key={index}>
                      <input
                        value={prompt}
                        onChange={(event) => updatePrompt(index, event.target.value)}
                        placeholder={t('agentManagement.form.promptPlaceholder')}
                      />
                      <button
                        type="button"
                        onClick={() => removePrompt(index)}
                        aria-label={t('agentManagement.form.removePrompt')}
                      >
                        <Minus size={16} aria-hidden="true" />
                      </button>
                    </div>
                  ))}
                </div>
              ) : null
            ) : null}
          </section>

          {error ? (
            <div className="agent-management-form-error agent-management-form-error--submit" role="alert">
              {error}
            </div>
          ) : null}
          <footer className="agent-management-editor__footer">
            <button
              type="button"
              className="agent-management-button agent-management-button--secondary"
              onClick={onCancel}
              disabled={saving}
            >
              {t('common.cancel')}
            </button>
            <button
              type="submit"
              className="agent-management-button agent-management-button--primary"
              disabled={saving}
            >
              {saving
                ? mode === 'edit'
                  ? t('agentManagement.actions.updating')
                  : t('common.saving')
                : t('common.confirm')}
            </button>
          </footer>

          {skillDialogOpen
            ? createPortal(
                <div
                  className="agent-management-selection-overlay"
                  role="presentation"
                  onMouseDown={(event) => {
                    if (event.target === event.currentTarget) setSkillDialogOpen(false);
                  }}
                >
                  <section
                    ref={skillDialogRef}
                    className="agent-management-selection-dialog"
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby="agent-skill-dialog-title"
                  >
                    <header>
                      <h2 id="agent-skill-dialog-title">{t('agentManagement.form.selectSkill')}</h2>
                      <button type="button" onClick={() => setSkillDialogOpen(false)} aria-label={t('common.cancel')}>
                        <X size={16} aria-hidden="true" />
                      </button>
                    </header>
                    <label className="agent-management-selection-search">
                      <SearchIcon aria-hidden="true" />
                      <input
                        type="search"
                        value={skillQuery}
                        onChange={(event) => setSkillQuery(event.target.value)}
                        placeholder={t('agentManagement.form.selectionSearchPlaceholder')}
                      />
                    </label>
                    <Tabs
                      className="agent-management-selection-source-tabs"
                      items={[
                        {
                          value: 'market',
                          label: t('agentManagement.form.skillMarket'),
                          testId: 'agent-editor-skill-picker-tab-market',
                        },
                        {
                          value: 'local',
                          label: t('agentManagement.form.mySkills'),
                          testId: 'agent-editor-skill-picker-tab-local',
                        },
                      ]}
                      value={skillSourceTab}
                      onChange={setSkillSourceTab}
                      wrapperTestId="agent-editor-skill-picker-tabs"
                      role="tablist"
                      ariaLabel={t('agentManagement.form.skillSourceTabsLabel')}
                    />
                    {selectionError ? (
                      <div
                        className="agent-management-form-error"
                        role="alert"
                        data-testid="agent-editor-selection-error"
                      >
                        {selectionError}
                      </div>
                    ) : null}
                    <div
                      className={`agent-management-selection-dialog__body${skillsStatus === 'success' && filteredSkills.length === 0 ? ' is-empty' : ''}`}
                    >
                      {skillsStatus === 'loading' ? (
                        <p className="agent-management-form-muted">{t('common.loading')}</p>
                      ) : null}
                      {skillsStatus === 'error' ? (
                        <div className="agent-management-form-error">
                          <span>{t('agentManagement.form.skillsError')}</span>
                          <button type="button" onClick={onReloadSkills}>
                            {t('common.retry')}
                          </button>
                        </div>
                      ) : null}
                      {skillsStatus === 'success' && filteredSkills.length === 0 ? (
                        <div className="agent-management-selection-empty-state">
                          <p>{t('agentManagement.form.skillsEmpty')}</p>
                        </div>
                      ) : null}
                      {skillsStatus === 'success' && filteredSkills.length > 0 ? (
                        <div className="agent-management-selection-grid">
                          {pageSkills.map((skill) => {
                            const selected = skillDraft.includes(skill.id);
                            const installed = skill.installed === true;
                            const installing = installingSkillId === skill.id;
                            return (
                              <PageCard
                                key={skill.id}
                                className={`agent-management-selection-card${selected ? ' is-selected' : ''}${!installed ? ' is-disabled' : ''}`}
                                testId="agent-editor-skill-picker-item"
                                variant={skill.id}
                                interactive={installed}
                                selected={selected}
                                disabled={!installed && !onInstallSkill}
                                ariaLabel={skill.name}
                                onClick={
                                  installed
                                    ? () =>
                                        setSkillDraft((current) =>
                                          selected ? current.filter((id) => id !== skill.id) : [...current, skill.id],
                                        )
                                    : undefined
                                }
                                avatar={{ name: skill.name }}
                                title={skill.name}
                                description={skill.description || t('agentManagement.unknownDescription')}
                                actionSlot={
                                  !installed && onInstallSkill ? (
                                    <button
                                      type="button"
                                      className="agent-management-inline-action agent-management-selection-card__install"
                                      data-testid="agent-editor-skill-picker-install"
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
                                    <span className="agent-management-selection-card__action" aria-hidden="true">
                                      {selected ? <Check size={12} strokeWidth={2.5} /> : null}
                                    </span>
                                  )
                                }
                              />
                            );
                          })}
                        </div>
                      ) : null}
                      {skillsStatus === 'success' ? (
                        <SelectionPagination
                          page={skillPage}
                          totalPages={skillTotalPages}
                          totalItems={filteredSkills.length}
                          onPageChange={setSkillPage}
                          testId="agent-editor-skill-picker-pagination"
                        />
                      ) : null}
                    </div>
                    <footer>
                      <span>{t('agentManagement.form.selectedCount', { count: skillDraft.length })}</span>
                      <div>
                        <button
                          type="button"
                          className="agent-management-button agent-management-button--secondary"
                          onClick={() => setSkillDialogOpen(false)}
                        >
                          {t('common.cancel')}
                        </button>
                        <button
                          type="button"
                          className="agent-management-button agent-management-button--primary"
                          onClick={() => {
                            update({ skillRefs: skillDraft });
                            setSkillDialogOpen(false);
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

          {mcpDialogOpen
            ? createPortal(
                <div
                  className="agent-management-selection-overlay"
                  role="presentation"
                  onMouseDown={(event) => {
                    if (event.target === event.currentTarget) setMcpDialogOpen(false);
                  }}
                >
                  <section
                    ref={mcpDialogRef}
                    className="agent-management-selection-dialog"
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby="agent-mcp-dialog-title"
                  >
                    <header>
                      <h2 id="agent-mcp-dialog-title">{t('agentManagement.form.selectMcp')}</h2>
                      <button type="button" onClick={() => setMcpDialogOpen(false)} aria-label={t('common.cancel')}>
                        <X size={16} aria-hidden="true" />
                      </button>
                    </header>
                    <Tabs
                      className="agent-management-selection-source-tabs"
                      items={[
                        {
                          value: 'market',
                          label: t('agentManagement.form.mcpMarket'),
                          testId: 'agent-editor-mcp-picker-tab-market',
                        },
                        {
                          value: 'installed',
                          label: t('agentManagement.form.myMcp'),
                          testId: 'agent-editor-mcp-picker-tab-installed',
                        },
                      ]}
                      value={mcpSourceTab}
                      onChange={setMcpSourceTab}
                      wrapperTestId="agent-editor-mcp-picker-tabs"
                      role="tablist"
                      ariaLabel={t('agentManagement.form.mcpSourceTabsLabel')}
                    />
                    {selectionError ? (
                      <div
                        className="agent-management-form-error"
                        role="alert"
                        data-testid="agent-editor-selection-error"
                      >
                        {selectionError}
                      </div>
                    ) : null}
                    <div className="agent-management-selection-controls">
                      <label className="agent-management-selection-search">
                        <SearchIcon aria-hidden="true" />
                        <input
                          type="search"
                          value={mcpQuery}
                          onChange={(event) => setMcpQuery(event.target.value)}
                          placeholder={t('agentManagement.form.selectionSearchPlaceholder')}
                        />
                      </label>
                    </div>
                    <div
                      className={`agent-management-selection-dialog__body${mcpStatus === 'success' && filteredMcps.length === 0 ? ' is-empty' : ''}`}
                    >
                      {mcpStatus === 'loading' ? (
                        <p className="agent-management-form-muted">{t('common.loading')}</p>
                      ) : null}
                      {mcpStatus === 'error' ? (
                        <div className="agent-management-form-error">
                          <span>{t('agentManagement.form.mcpError')}</span>
                          <button type="button" onClick={onReloadMcps}>
                            {t('common.retry')}
                          </button>
                        </div>
                      ) : null}
                      {mcpStatus === 'success' && filteredMcps.length === 0 ? (
                        <div className="agent-management-selection-empty-state">
                          <p>{t('agentManagement.form.mcpEmpty')}</p>
                        </div>
                      ) : null}
                      {mcpStatus === 'success' && filteredMcps.length > 0 ? (
                        <div className="agent-management-selection-grid">
                          {pageMcps.map((mcp) => {
                            const selected = mcpDraft.includes(mcp.id);
                            const installed = mcp.installed === true;
                            const selectable = isMcpSelectable(mcp);
                            const unconnected = installed && !selectable;
                            const connecting = connectingMcpId === mcp.id || mcp.connectionState === 'connecting';
                            const installing = installingMcpId === mcp.id;
                            return (
                              <PageCard
                                key={mcp.id}
                                className={`agent-management-selection-card${selected ? ' is-selected' : ''}${!selectable ? ' is-disabled' : ''}`}
                                testId="agent-editor-mcp-picker-item"
                                variant={mcp.id}
                                interactive={selectable}
                                selected={selected}
                                disabled={!selectable && !onInstallMcp && !onConnectMcp}
                                ariaLabel={mcp.name}
                                onClick={
                                  selectable
                                    ? () =>
                                        setMcpDraft((current) =>
                                          selected ? current.filter((id) => id !== mcp.id) : [...current, mcp.id],
                                        )
                                    : undefined
                                }
                                avatar={{ name: mcp.name, iconUrl: mcp.icon || undefined }}
                                title={mcp.name}
                                description={mcp.description || t('agentManagement.unknownDescription')}
                                actionSlot={
                                  !installed && onInstallMcp ? (
                                    <button
                                      type="button"
                                      className="agent-management-inline-action agent-management-selection-card__install"
                                      data-testid="agent-editor-mcp-picker-install"
                                      data-variant={mcp.id}
                                      disabled={installing}
                                      aria-busy={installing}
                                      onClick={(event) => {
                                        event.stopPropagation();
                                        void onInstallMcp(mcp);
                                      }}
                                    >
                                      {installing
                                        ? t('agentManagement.form.installingConnector')
                                        : t('agentManagement.form.installConnector')}
                                    </button>
                                  ) : unconnected && onConnectMcp ? (
                                    <button
                                      type="button"
                                      className="agent-management-inline-action agent-management-selection-card__install"
                                      data-testid="agent-editor-mcp-picker-connect"
                                      data-variant={mcp.id}
                                      disabled={connecting}
                                      aria-busy={connecting}
                                      onClick={(event) => {
                                        event.stopPropagation();
                                        onConnectMcp(mcp);
                                      }}
                                    >
                                      {connecting
                                        ? t('agentManagement.form.connectingConnector')
                                        : t('agentManagement.form.connectConnector')}
                                    </button>
                                  ) : (
                                    <span className="agent-management-selection-card__action" aria-hidden="true">
                                      {selected ? <Check size={12} strokeWidth={2.5} /> : null}
                                    </span>
                                  )
                                }
                              />
                            );
                          })}
                        </div>
                      ) : null}
                      {mcpStatus === 'success' ? (
                        <SelectionPagination
                          page={mcpPage}
                          totalPages={mcpTotalPages}
                          totalItems={filteredMcps.length}
                          onPageChange={setMcpPage}
                          testId="agent-editor-mcp-picker-pagination"
                        />
                      ) : null}
                    </div>
                    <footer>
                      <span>{t('agentManagement.form.selectedCount', { count: mcpDraft.length })}</span>
                      <div>
                        <button
                          type="button"
                          className="agent-management-button agent-management-button--secondary"
                          onClick={() => setMcpDialogOpen(false)}
                        >
                          {t('common.cancel')}
                        </button>
                        <button
                          type="button"
                          className="agent-management-button agent-management-button--primary"
                          onClick={() => {
                            update({ mcpRefs: mcpDraft });
                            setMcpDialogOpen(false);
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
        </div>
      </div>
    </form>
  );
}
