import { Check, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Minus, X } from 'lucide-react';
import { createPortal } from 'react-dom';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import AddIcon from '../../assets/agent-management/add.svg?react';
import DeleteIcon from '../../assets/agent-management/remove.svg?react';
import PlusIcon from '../../assets/agent-management/agent-plus.svg?react';
import SearchIcon from '../../assets/agent-management/agent-search.svg?react';
import UninstallIcon from '../../assets/agent-management/uninstall.svg?react';
import BackIcon from '../../assets/work-mode/arrow-left.svg?react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { AgentDraft, McpOption, RequestStatus, SkillOption } from '../../features/agentManagement';
import { AGENT_TAG_OPTIONS } from '../../features/agentManagement/tagOptions';

type AgentEditorProps = {
  draft: AgentDraft;
  skillOptions: SkillOption[];
  skillsStatus: RequestStatus;
  mcpOptions: McpOption[];
  mcpStatus: RequestStatus;
  saving: boolean;
  error: string | null;
  onChange: (draft: AgentDraft) => void;
  onReloadSkills: () => void;
  onReloadMcps: () => void;
  onCancel: () => void;
  onSave: () => void;
};

const AGENT_NAME_MAX_LENGTH = 50;
const AGENT_DESCRIPTION_MAX_LENGTH = 2000;

const MCP_TYPE_OPTIONS = [
  ['stdio-mcp', 'connectorMarket.detail.integrationType.stdioMcp'],
  ['remote-mcp', 'connectorMarket.detail.integrationType.remoteMcp'],
  ['cli', 'connectorMarket.detail.integrationType.cli'],
  ['skill-only', 'connectorMarket.detail.integrationType.skillOnly'],
] as const;

export function AgentEditor({
  draft,
  skillOptions,
  skillsStatus,
  mcpOptions,
  mcpStatus,
  saving,
  error,
  onChange,
  onReloadSkills,
  onReloadMcps,
  onCancel,
  onSave,
}: AgentEditorProps) {
  const { t } = useTranslation();
  const [touched, setTouched] = useState(false);
  const [tagMenuOpen, setTagMenuOpen] = useState(false);
  const [canScrollTagsLeft, setCanScrollTagsLeft] = useState(false);
  const [canScrollTagsRight, setCanScrollTagsRight] = useState(false);
  const [mcpOpen, setMcpOpen] = useState(true);
  const [skillsOpen, setSkillsOpen] = useState(true);
  const [promptsOpen, setPromptsOpen] = useState(true);
  const [personaEditing, setPersonaEditing] = useState(false);
  const [skillDialogOpen, setSkillDialogOpen] = useState(false);
  const [mcpDialogOpen, setMcpDialogOpen] = useState(false);
  const [skillQuery, setSkillQuery] = useState('');
  const [mcpQuery, setMcpQuery] = useState('');
  const [customTagInput, setCustomTagInput] = useState('');
  const [mcpType, setMcpType] = useState('');
  const [mcpTypeOpen, setMcpTypeOpen] = useState(false);
  const [skillDraft, setSkillDraft] = useState<string[]>(draft.skillRefs);
  const [mcpDraft, setMcpDraft] = useState<string[]>(draft.mcpRefs);
  const tagPickerRef = useRef<HTMLDivElement>(null);
  const tagValuesRef = useRef<HTMLSpanElement>(null);
  const personaSurfaceRef = useRef<HTMLDivElement>(null);
  const mcpTypeRef = useRef<HTMLDivElement>(null);
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
  const selectedSkills = skillOptions.filter(skill => draft.skillRefs.includes(skill.id));
  const selectedMcps = mcpOptions.filter(mcp => draft.mcpRefs.includes(mcp.id));
  const filteredSkills = skillOptions.filter(skill => `${skill.name} ${skill.description}`.toLocaleLowerCase().includes(skillQuery.trim().toLocaleLowerCase()));
  const filteredMcps = mcpOptions.filter(mcp => {
    const matchesQuery = `${mcp.name} ${mcp.description}`.toLocaleLowerCase().includes(mcpQuery.trim().toLocaleLowerCase());
    const matchesType = !mcpType || mcp.integrationType === mcpType;
    return matchesQuery && matchesType;
  });
  const selectedMcpType = MCP_TYPE_OPTIONS.find(([value]) => value === mcpType);
  const tagValueSignature = `${draft.tagIds.join('\u0000')}\u0001${draft.customTags.join('\u0000')}`;

  const updateTagScrollState = useCallback(() => {
    const values = tagValuesRef.current;
    if (!values) return;
    setCanScrollTagsLeft(values.scrollLeft > 1);
    setCanScrollTagsRight(values.scrollLeft < values.scrollWidth - values.clientWidth - 1);
  }, []);

  useLayoutEffect(() => {
    const values = tagValuesRef.current;
    if (!values) return;
    updateTagScrollState();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(updateTagScrollState);
    observer.observe(values);
    return () => observer.disconnect();
  }, [tagValueSignature, updateTagScrollState]);

  useEffect(() => {
    if (!tagMenuOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (!tagPickerRef.current?.contains(event.target as Node)) setTagMenuOpen(false);
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [tagMenuOpen]);

  useEffect(() => {
    if (!personaEditing) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (!personaSurfaceRef.current?.contains(event.target as Node)) setPersonaEditing(false);
    };
    document.addEventListener('pointerdown', handlePointerDown, true);
    return () => document.removeEventListener('pointerdown', handlePointerDown, true);
  }, [personaEditing]);

  useEffect(() => {
    if (!mcpTypeOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (!mcpTypeRef.current?.contains(event.target as Node)) setMcpTypeOpen(false);
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [mcpTypeOpen]);

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

  const toggleTag = (tagId: string) => {
    const tagIds = draft.tagIds.includes(tagId) ? draft.tagIds.filter(item => item !== tagId) : [...draft.tagIds, tagId];
    update({ tagIds });
  };

  const addCustomTag = () => {
    const value = customTagInput.trim();
    if (!value || draft.customTags.includes(value)) return;
    update({ customTags: [...draft.customTags, value] });
    setCustomTagInput('');
  };

  const removeCustomTag = (tag: string) => {
    update({ customTags: draft.customTags.filter(item => item !== tag) });
  };

  const scrollTagValues = (direction: 1 | -1) => {
    const values = tagValuesRef.current;
    if (!values) return;
    values.scrollBy({ left: direction * values.clientWidth * 0.8, behavior: 'smooth' });
  };

  const openSkillDialog = () => {
    setSkillDraft(draft.skillRefs);
    setSkillQuery('');
    setSkillDialogOpen(true);
  };

  const openMcpDialog = () => {
    setMcpDraft(draft.mcpRefs);
    setMcpQuery('');
    setMcpType('');
    setMcpTypeOpen(false);
    setMcpDialogOpen(true);
  };

  const updatePrompt = (index: number, value: string) => {
    const suggestedPrompts = draft.suggestedPrompts.map((prompt, promptIndex) => (promptIndex === index ? value : prompt));
    update({ suggestedPrompts });
  };

  const addPrompt = () => {
    if (draft.suggestedPrompts.some(prompt => prompt.trim().length === 0)) return;
    update({ suggestedPrompts: [...draft.suggestedPrompts, ''] });
  };

  const removePrompt = (index: number) => update({ suggestedPrompts: draft.suggestedPrompts.filter((_, promptIndex) => promptIndex !== index) });

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (!hasErrors) onSave();
  };

  return (
    <form className="agent-management-editor" onSubmit={handleSubmit} data-testid="agent-editor">
      <button type="button" className="detail-back mb-[35px]" onClick={onCancel}>
        <BackIcon aria-hidden="true" />
        {t('agentManagement.actions.back')}
      </button>

      <div className="detail-body flex-1 min-h-0 overflow-y-auto">
      <div className="agent-management-editor__inner">
      <header className="agent-management-editor__header">
        <h1>{t('agentManagement.form.title')}</h1>
        <div className="agent-management-editor__tabs" role="tablist" aria-label={t('agentManagement.form.createTabsLabel')}>
          <span className="is-active" role="tab" aria-selected="true">
            {t('agentManagement.form.createAgentTab')}
          </span>
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
              onChange={event => update({ name: event.target.value })}
              placeholder={t('agentManagement.form.namePlaceholder')}
              maxLength={AGENT_NAME_MAX_LENGTH}
              aria-invalid={Boolean(touched && errors.name)}
            />
            {touched && errors.name ? <small className="agent-management-field-error">{errors.name}</small> : null}
          </div>
          <div className="agent-management-form-field--wide">
            <div className="agent-management-form-field__label-row">
              <label htmlFor="agent-management-agent-description">{t('agentManagement.form.descriptionLabel')}</label>
              <span
                aria-hidden="true"
                className={`agent-management-field-counter${draft.description.length >= AGENT_DESCRIPTION_MAX_LENGTH ? ' is-limit' : ''}`}
                data-testid="agent-editor-description-counter"
              >
                {t('agentManagement.form.charCount', { count: draft.description.length, max: AGENT_DESCRIPTION_MAX_LENGTH })}
              </span>
            </div>
            <textarea
              id="agent-management-agent-description"
              rows={2}
              value={draft.description}
              onChange={event => update({ description: event.target.value })}
              placeholder={t('agentManagement.form.descriptionPlaceholder')}
              maxLength={AGENT_DESCRIPTION_MAX_LENGTH}
              aria-invalid={Boolean(touched && errors.description)}
            />
            {touched && errors.description ? <small className="agent-management-field-error">{errors.description}</small> : null}
          </div>
          <div className="agent-management-form-field--wide agent-management-form-field--tag-picker" ref={tagPickerRef}>
            <span>{t('agentManagement.form.tagLabel')}</span>
            <div className="agent-management-tag-picker">
              <div
                className="agent-management-tag-picker__trigger"
                data-empty={draft.tagIds.length === 0 && draft.customTags.length === 0}
                onClick={event => {
                  if ((event.target as HTMLElement).closest('button')) return;
                  setTagMenuOpen(open => !open);
                }}
              >
                <button
                  type="button"
                  className="agent-management-tag-picker__scroll agent-management-tag-picker__scroll--prev"
                  aria-label={t('agentManagement.form.tagScrollPrev')}
                  data-hidden={!canScrollTagsLeft}
                  onClick={event => {
                    event.stopPropagation();
                    scrollTagValues(-1);
                  }}
                >
                  <ChevronLeft size={16} aria-hidden="true" />
                </button>
                <span ref={tagValuesRef} className="agent-management-tag-picker__values" onScroll={updateTagScrollState}>
                  {draft.tagIds.length > 0 || draft.customTags.length > 0 ? (
                    <>
                    {draft.tagIds.map(tagId => {
                      const option = AGENT_TAG_OPTIONS.find(item => item.id === tagId);
                      if (!option) return null;
                      return (
                        <span key={tagId} className="agent-management-tag agent-management-tag--selected">
                          <span>{t(option.labelKey)}</span>
                          <button
                            type="button"
                            aria-label={t('agentManagement.form.removeTag', { name: t(option.labelKey) })}
                            onClick={(event) => {
                              event.stopPropagation();
                              toggleTag(tagId);
                            }}
                          >
                            <span aria-hidden="true">×</span>
                          </button>
                        </span>
                      );
                    })}
                    {draft.customTags.map(tag => (
                      <span key={tag} className="agent-management-tag agent-management-tag--selected">
                        <span>{tag}</span>
                        <button
                          type="button"
                          aria-label={t('agentManagement.form.removeCustomTag', { name: tag })}
                          onClick={(event) => {
                            event.stopPropagation();
                            removeCustomTag(tag);
                          }}
                        >
                          <span aria-hidden="true">×</span>
                        </button>
                      </span>
                    ))}
                    </>
                  ) : (
                    <span className="agent-management-form-placeholder">{t('agentManagement.form.tagPlaceholder')}</span>
                  )}
                </span>
                <button
                  type="button"
                  className="agent-management-tag-picker__scroll agent-management-tag-picker__scroll--next"
                  aria-label={t('agentManagement.form.tagScrollNext')}
                  data-hidden={!canScrollTagsRight}
                  onClick={event => {
                    event.stopPropagation();
                    scrollTagValues(1);
                  }}
                >
                  <ChevronRight size={16} aria-hidden="true" />
                </button>
                <button
                  type="button"
                  className="agent-management-tag-picker__toggle"
                  aria-label={t('agentManagement.form.toggleTags')}
                  aria-expanded={tagMenuOpen}
                  aria-haspopup="listbox"
                  onClick={event => {
                    event.stopPropagation();
                    setTagMenuOpen(open => !open);
                  }}
                >
                  <ChevronDown className="agent-management-tag-picker__chevron" size={16} aria-hidden="true" />
                </button>
              </div>
              {tagMenuOpen ? (
                <div className="agent-management-tag-picker__options" role="listbox" aria-label={t('agentManagement.form.tagLabel')}>
                  {AGENT_TAG_OPTIONS.map(option => {
                    const selected = draft.tagIds.includes(option.id);
                    return (
                      <button
                        key={option.id}
                        type="button"
                        role="option"
                        aria-selected={selected}
                        className={selected ? 'is-selected' : ''}
                        onClick={() => toggleTag(option.id)}
                      >
                        <span>{t(option.labelKey)}</span>
                        {selected ? <Check size={14} aria-hidden="true" /> : null}
                      </button>
                    );
                  })}
                  <div className="agent-management-tag-picker__custom">
                    <input
                      value={customTagInput}
                      onChange={event => setCustomTagInput(event.target.value)}
                      onKeyDown={event => {
                        if (event.key === 'Enter') {
                          event.preventDefault();
                          addCustomTag();
                        }
                      }}
                      placeholder={t('agentManagement.form.customTagPlaceholder')}
                      aria-label={t('agentManagement.form.customTagPlaceholder')}
                    />
                    <button type="button" onClick={addCustomTag} disabled={!customTagInput.trim()}>
                      <PlusIcon aria-hidden="true" />
                      {t('agentManagement.form.addCustomTag')}
                    </button>
                  </div>
                </div>
              ) : null}
            </div>
          </div>
          <div className="agent-management-form-field--wide agent-management-persona-field">
            <span>{t('agentManagement.form.personaLabel')}</span>
            <div className="agent-management-persona-surface" ref={personaSurfaceRef}>
              {personaEditing ? (
              <textarea
                className="agent-management-persona"
                rows={12}
                value={draft.persona}
                onChange={event => update({ persona: event.target.value })}
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
                  onKeyDown={event => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      setPersonaEditing(true);
                    }
                  }}
                >
                  {draft.persona.trim() ? <div className="agent-management-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{draft.persona}</ReactMarkdown></div> : <span className="agent-management-persona-placeholder">{t('agentManagement.form.personaPlaceholder')}</span>}
                </div>
              )}
            </div>
            {touched && errors.persona ? <small className="agent-management-field-error">{errors.persona}</small> : null}
          </div>
        </div>
      </section>

      <section className="agent-management-form-section agent-management-form-section--mcp">
        <div className="agent-management-form-section__header">
          <div className="agent-management-form-section__heading">
            <button type="button" className="agent-management-section-toggle" aria-expanded={mcpOpen} aria-label={t('agentManagement.form.mcpToggle')} onClick={() => setMcpOpen(open => !open)}>
              {mcpOpen ? <ChevronUp size={18} aria-hidden="true" /> : <ChevronDown size={18} aria-hidden="true" />}
            </button>
            <div><h2>{t('agentManagement.form.mcpLabel')}</h2></div>
          </div>
          <button ref={mcpDialogTriggerRef} type="button" className="agent-management-inline-action" onClick={openMcpDialog}><PlusIcon aria-hidden="true" />{t('agentManagement.form.addMcp')}</button>
        </div>
        {mcpOpen ? (
          selectedMcps.length > 0 ? (
            <div className="agent-management-selected-capabilities">
              {selectedMcps.map(mcp => (
                <article className="agent-management-capability-card" key={mcp.id}>
                  <div className="agent-management-capability-card__heading">
                    <span className="agent-management-capability-card__icon">{mcp.name.slice(0, 1).toUpperCase()}</span>
                    <strong>{mcp.name}</strong>
                    <button type="button" className="agent-management-capability-card__remove" aria-label={t('agentManagement.form.removeMcp', { name: mcp.name })} onClick={() => update({ mcpRefs: draft.mcpRefs.filter(id => id !== mcp.id) })}>
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
            <button type="button" className="agent-management-section-toggle" aria-expanded={skillsOpen} aria-label={t('agentManagement.form.skillsToggle')} onClick={() => setSkillsOpen(open => !open)}>
              {skillsOpen ? <ChevronUp size={18} aria-hidden="true" /> : <ChevronDown size={18} aria-hidden="true" />}
            </button>
            <div><h2>{t('agentManagement.form.skillsLabel')}</h2></div>
          </div>
          <button ref={skillDialogTriggerRef} type="button" className="agent-management-inline-action" onClick={openSkillDialog}><PlusIcon aria-hidden="true" />{t('agentManagement.form.addSkill')}</button>
        </div>
        {skillsOpen ? (
          <>
            {skillsStatus === 'loading' ? <p className="agent-management-form-muted">{t('common.loading')}</p> : null}
            {skillsStatus === 'error' ? <div className="agent-management-form-error"><span>{t('agentManagement.form.skillsError')}</span><button type="button" onClick={onReloadSkills}>{t('common.retry')}</button></div> : null}
            {selectedSkills.length > 0 ? (
              <div className="agent-management-selected-capabilities">
                {selectedSkills.map(skill => (
                  <article className="agent-management-capability-card" key={skill.id}>
                    <div className="agent-management-capability-card__heading">
                      <span className="agent-management-capability-card__icon">{skill.name.slice(0, 1).toUpperCase()}</span>
                      <strong>{skill.name}</strong>
                      <button type="button" className="agent-management-capability-card__remove" aria-label={t('agentManagement.form.removeSkill', { name: skill.name })} onClick={() => update({ skillRefs: draft.skillRefs.filter(id => id !== skill.id) })}><UninstallIcon aria-hidden="true" /></button>
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
            <button type="button" className="agent-management-section-toggle" aria-expanded={promptsOpen} aria-label={t('agentManagement.form.promptsToggle')} onClick={() => setPromptsOpen(open => !open)}>
              {promptsOpen ? <ChevronUp size={18} aria-hidden="true" /> : <ChevronDown size={18} aria-hidden="true" />}
            </button>
            <h2>{t('agentManagement.form.promptsLabel')}</h2>
          </div>
          <button type="button" className="agent-management-inline-action" onClick={addPrompt}><PlusIcon aria-hidden="true" />{t('agentManagement.form.addPrompt')}</button>
        </div>
        {promptsOpen ? (
          draft.suggestedPrompts.length > 0 ? (
            <div className="agent-management-prompt-editor-list">
              {draft.suggestedPrompts.map((prompt, index) => (
                <div className="agent-management-prompt-editor" key={index}>
                  <input value={prompt} onChange={event => updatePrompt(index, event.target.value)} placeholder={t('agentManagement.form.promptPlaceholder')} />
                  <button type="button" onClick={() => removePrompt(index)} aria-label={t('agentManagement.form.removePrompt')}><Minus size={16} aria-hidden="true" /></button>
                </div>
              ))}
            </div>
          ) : null
        ) : null}
      </section>

      {error ? <div className="agent-management-form-error agent-management-form-error--submit" role="alert">{error}</div> : null}
      <footer className="agent-management-editor__footer">
        <button type="button" className="agent-management-button agent-management-button--secondary" onClick={onCancel} disabled={saving}>{t('common.cancel')}</button>
        <button type="submit" className="agent-management-button agent-management-button--primary" disabled={saving}>{saving ? t('common.saving') : t('common.confirm')}</button>
      </footer>

      {skillDialogOpen ? createPortal(
        <div className="agent-management-selection-overlay" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) setSkillDialogOpen(false); }}>
          <section ref={skillDialogRef} className="agent-management-selection-dialog" role="dialog" aria-modal="true" aria-labelledby="agent-skill-dialog-title">
            <header><h2 id="agent-skill-dialog-title">{t('agentManagement.form.selectSkill')}</h2><button type="button" onClick={() => setSkillDialogOpen(false)} aria-label={t('common.cancel')}><X size={16} aria-hidden="true" /></button></header>
            <label className="agent-management-selection-search">
              <SearchIcon aria-hidden="true" />
              <input type="search" value={skillQuery} onChange={event => setSkillQuery(event.target.value)} placeholder={t('agentManagement.form.selectionSearchPlaceholder')} />
            </label>
            <div className={`agent-management-selection-dialog__body${skillsStatus === 'success' && filteredSkills.length === 0 ? ' is-empty' : ''}`}>
              {skillsStatus === 'loading' ? <p className="agent-management-form-muted">{t('common.loading')}</p> : null}
              {skillsStatus === 'error' ? <div className="agent-management-form-error"><span>{t('agentManagement.form.skillsError')}</span><button type="button" onClick={onReloadSkills}>{t('common.retry')}</button></div> : null}
              {skillsStatus === 'success' && filteredSkills.length === 0 ? <div className="agent-management-selection-empty-state"><p>{t('agentManagement.form.skillsEmpty')}</p></div> : null}
              {skillsStatus === 'success' && filteredSkills.length > 0 ? <div className="agent-management-selection-grid">{filteredSkills.map(skill => {
                const selected = skillDraft.includes(skill.id);
                return <button key={skill.id} type="button" className={`agent-management-selection-card${selected ? ' is-selected' : ''}`} onClick={() => setSkillDraft(current => selected ? current.filter(id => id !== skill.id) : [...current, skill.id])} aria-pressed={selected}><span className="agent-management-capability-card__icon">{skill.name.slice(0, 1).toUpperCase()}</span><span><strong>{skill.name}</strong><small>{skill.description}</small></span><span className="agent-management-selection-card__action" aria-hidden="true">{selected ? <DeleteIcon /> : <AddIcon />}</span></button>;
              })}</div> : null}
            </div>
            <footer><span>{t('agentManagement.form.selectedCount', { count: skillDraft.length })}</span><div><button type="button" className="agent-management-button agent-management-button--secondary" onClick={() => setSkillDialogOpen(false)}>{t('common.cancel')}</button><button type="button" className="agent-management-button agent-management-button--primary" onClick={() => { update({ skillRefs: skillDraft }); setSkillDialogOpen(false); }}>{t('common.confirm')}</button></div></footer>
          </section>
        </div>,
        document.body,
      ) : null}

      {mcpDialogOpen ? createPortal(
        <div className="agent-management-selection-overlay" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) setMcpDialogOpen(false); }}>
          <section ref={mcpDialogRef} className="agent-management-selection-dialog" role="dialog" aria-modal="true" aria-labelledby="agent-mcp-dialog-title">
            <header><h2 id="agent-mcp-dialog-title">{t('agentManagement.form.selectMcp')}</h2><button type="button" onClick={() => setMcpDialogOpen(false)} aria-label={t('common.cancel')}><X size={16} aria-hidden="true" /></button></header>
            <div className="agent-management-selection-controls">
              <div className="agent-management-selection-filter" ref={mcpTypeRef}>
                <button type="button" className="agent-management-selection-filter__trigger" aria-haspopup="listbox" aria-expanded={mcpTypeOpen} onClick={() => setMcpTypeOpen(open => !open)}>
                  <span>{selectedMcpType ? t(selectedMcpType[1]) : t('agentManagement.form.mcpTypeAll')}</span>
                  <ChevronDown size={14} aria-hidden="true" />
                </button>
                {mcpTypeOpen ? <div className="agent-management-selection-filter__menu" role="listbox" aria-label={t('agentManagement.form.mcpTypeFilter')}>
                  <button type="button" role="option" aria-selected={!mcpType} onClick={() => { setMcpType(''); setMcpTypeOpen(false); }}>{t('agentManagement.form.mcpTypeAll')}{!mcpType ? <Check size={14} aria-hidden="true" /> : null}</button>
                  {MCP_TYPE_OPTIONS.map(([value, labelKey]) => <button type="button" role="option" aria-selected={mcpType === value} key={value} onClick={() => { setMcpType(value); setMcpTypeOpen(false); }}>{t(labelKey)}{mcpType === value ? <Check size={14} aria-hidden="true" /> : null}</button>)}
                </div> : null}
              </div>
              <label className="agent-management-selection-search">
                <SearchIcon aria-hidden="true" />
                <input type="search" value={mcpQuery} onChange={event => setMcpQuery(event.target.value)} placeholder={t('agentManagement.form.selectionSearchPlaceholder')} />
              </label>
            </div>
            <div className={`agent-management-selection-dialog__body${mcpStatus === 'success' && filteredMcps.length === 0 ? ' is-empty' : ''}`}>
              {mcpStatus === 'loading' ? <p className="agent-management-form-muted">{t('common.loading')}</p> : null}
              {mcpStatus === 'error' ? <div className="agent-management-form-error"><span>{t('agentManagement.form.mcpError')}</span><button type="button" onClick={onReloadMcps}>{t('common.retry')}</button></div> : null}
              {mcpStatus === 'success' && filteredMcps.length === 0 ? <div className="agent-management-selection-empty-state"><p>{t('agentManagement.form.mcpEmpty')}</p></div> : null}
              {mcpStatus === 'success' && filteredMcps.length > 0 ? <div className="agent-management-selection-grid">{filteredMcps.map(mcp => {
                const selected = mcpDraft.includes(mcp.id);
                return <button key={mcp.id} type="button" className={`agent-management-selection-card${selected ? ' is-selected' : ''}`} onClick={() => setMcpDraft(current => selected ? current.filter(id => id !== mcp.id) : [...current, mcp.id])} aria-pressed={selected}><span className="agent-management-capability-card__icon">{mcp.name.slice(0, 1).toUpperCase()}</span><span><strong>{mcp.name}</strong><small>{mcp.description}</small></span><span className="agent-management-selection-card__action" aria-hidden="true">{selected ? <DeleteIcon /> : <AddIcon />}</span></button>;
              })}</div> : null}
            </div>
            <footer><span>{t('agentManagement.form.selectedCount', { count: mcpDraft.length })}</span><div><button type="button" className="agent-management-button agent-management-button--secondary" onClick={() => setMcpDialogOpen(false)}>{t('common.cancel')}</button><button type="button" className="agent-management-button agent-management-button--primary" onClick={() => { update({ mcpRefs: mcpDraft }); setMcpDialogOpen(false); }}>{t('common.confirm')}</button></div></footer>
          </section>
        </div>,
        document.body,
      ) : null}
      </div>
      </div>
    </form>
  );
}
