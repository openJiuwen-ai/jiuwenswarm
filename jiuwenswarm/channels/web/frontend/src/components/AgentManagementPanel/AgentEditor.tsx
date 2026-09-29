import { ChevronDown, ChevronUp, Minus, Plus } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import SearchIcon from '../../assets/agent-management/agent-search.svg?react';
import EntityAddIcon from '../../assets/agent-management/add.svg?react';
import EntityRemoveIcon from '../../assets/agent-management/remove.svg?react';
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
import { DeleteCardIcon } from './cardActions';
import { PageCard, Tabs, Input, Textarea, FormDrawer, FieldError, SelectedCount } from '../ui';
import type { PageCardDefaultButton } from '../ui';
// 深引入而非 ../ui barrel：本组件经 esbuild 独立打包进测试
import { LoadingSpinner } from '../ui/LoadingSpinner/LoadingSpinner';
import { FormPageLayout } from '../ConnectorMarket/FormPageLayout';

/** 技能选择列表首批渲染数量；触底后每批追加同数量。 */
const SKILL_PICKER_BATCH_SIZE = 30;
/** 触底判定余量：距滚动底部不足该像素即视为到底。 */
const LOAD_MORE_THRESHOLD_PX = 40;

type AgentEditorProps = {
  draft: AgentDraft;
  mode?: 'create' | 'edit';
  skillOptions: SkillOption[];
  skillsStatus: RequestStatus;
  mcpOptions: McpOption[];
  mcpStatus: RequestStatus;
  saving: boolean;
  error: string | null;
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
  const personaTextareaRef = useRef<HTMLTextAreaElement>(null);

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
  const skillListScrollRef = useRef<HTMLDivElement | null>(null);
  const [skillVisibleCount, setSkillVisibleCount] = useState(SKILL_PICKER_BATCH_SIZE);
  const skillHasMore = skillVisibleCount < filteredSkills.length;
  const visibleSkills = filteredSkills.slice(0, skillVisibleCount);

  // 切换来源页签/搜索词后回到首批，对齐原分页的重置行为
  useEffect(() => {
    setSkillVisibleCount(SKILL_PICKER_BATCH_SIZE);
  }, [skillSourceTab, skillQuery]);

  const appendNextSkillBatch = useCallback(() => {
    setSkillVisibleCount((count) =>
      count < filteredSkills.length ? Math.min(count + SKILL_PICKER_BATCH_SIZE, filteredSkills.length) : count,
    );
  }, [filteredSkills.length]);

  // 滚动触底：底部加载组件已可见，追加下一批（技能列表在内存中，追加为同步展示）
  const handleSkillListScroll = useCallback(() => {
    const el = skillListScrollRef.current;
    if (!el || !skillHasMore) return;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - LOAD_MORE_THRESHOLD_PX) appendNextSkillBatch();
  }, [appendNextSkillBatch, skillHasMore]);

  useEffect(() => {
    if (!personaEditing) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (!personaSurfaceRef.current?.contains(event.target as Node)) setPersonaEditing(false);
    };
    document.addEventListener('pointerdown', handlePointerDown, true);
    return () => document.removeEventListener('pointerdown', handlePointerDown, true);
  }, [personaEditing]);

  useEffect(() => {
    const el = personaTextareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = el.scrollHeight + 'px';
  }, [draft.persona, personaEditing]);

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
    <>
      <FormPageLayout
        onBack={onCancel}
        title={
          <div className="flex flex-col gap-2">
            <span>{mode === 'edit' ? t('agentManagement.form.editTitle') : t('agentManagement.form.title')}</span>
            <Tabs
              value="agent"
              onChange={() => onCreateGroup?.()}
              items={[
                { value: 'agent', label: t('agentManagement.form.createAgentTab'), testId: 'agent-editor-agent-tab' },
                {
                  value: 'group',
                  label: t('agentManagement.group.form.createGroupTab'),
                  testId: 'agent-editor-group-tab',
                },
              ]}
              wrapperTestId="agent-management-editor-tabs"
              itemTestId="agent-management-editor-tab"
              role="tablist"
              ariaLabel={t('agentManagement.form.createTabsLabel')}
            />
          </div>
        }
        testId="agent-management-editor"
        onConfirm={() => {
          setTouched(true);
          if (!hasErrors) onSave();
        }}
        cancelLabel={t('common.cancel')}
        confirmLabel={
          saving ? (mode === 'edit' ? t('agentManagement.actions.updating') : t('common.saving')) : t('common.confirm')
        }
        confirmLoading={saving}
        footerSlot={
          error ? (
            <div className="agent-management-form-error" role="alert" data-testid="agent-management-editor-error">
              {error}
            </div>
          ) : null
        }
      >
        <form onSubmit={handleSubmit} data-testid="agent-editor-form">
          <Section title={t('agentManagement.form.basic')}>
            <div className="mb-4">
              <label className="mb-1.5 block text-[13px] font-medium text-text">
                {t('agentManagement.form.nameLabel')}
              </label>
              <Input
                value={draft.name}
                onChange={(value) => update({ name: value })}
                placeholder={t('agentManagement.form.namePlaceholder')}
                invalid={Boolean(touched && errors.name)}
                data-testid="agent-editor-name"
                maxLength={AGENT_NAME_MAX_LENGTH}
                showCounter
                counterTestId="agent-editor-name-counter"
              />
              <FieldError className="mt-1" testId="agent-editor-name-error">
                {touched && errors.name ? errors.name : null}
              </FieldError>
            </div>

            <div className="mb-4">
              <label className="mb-1.5 block text-[13px] font-medium text-text">
                {t('agentManagement.form.descriptionLabel')}
              </label>
              <Textarea
                value={draft.description}
                onChange={(value) => update({ description: value })}
                placeholder={t('agentManagement.form.descriptionPlaceholder')}
                rows={2}
                invalid={Boolean(touched && errors.description)}
                data-testid="agent-editor-description"
                maxLength={AGENT_DESCRIPTION_MAX_LENGTH}
                showCounter
                counterTestId="agent-editor-description-counter"
                scrollable
              />
              <FieldError className="mt-1" testId="agent-editor-description-error">
                {touched && errors.description ? errors.description : null}
              </FieldError>
            </div>

            <div className="mb-4">
              <label className="mb-1.5 block text-[13px] font-medium text-text">
                {t('agentManagement.form.tagLabel')}
              </label>
              <AgentTagPicker
                tagIds={draft.tagIds}
                customTags={draft.customTags}
                label={t('agentManagement.form.tagLabel')}
                placeholder={t('agentManagement.form.tagPlaceholder')}
                onChange={(value) => update(value)}
              />
            </div>

            <div className="mb-4">
              <label className="mb-1.5 block text-[13px] font-medium text-text">
                {t('agentManagement.form.personaLabel')}
              </label>
              <div className="ui-textarea-root">
                <div
                  ref={personaSurfaceRef}
                  className={`ui-textarea-wrapper h-[280px] min-h-[100px] resize-vertical${touched && errors.persona ? ' ui-textarea-wrapper--invalid' : ''}`}
                >
                  <div className="ui-textarea-scroll-container">
                    {personaEditing ? (
                      <textarea
                        ref={personaTextareaRef}
                        className="ui-textarea-scrollable whitespace-pre-wrap text-[14px] tracking-[0]"
                        value={draft.persona}
                        onChange={(e) => update({ persona: e.target.value })}
                        placeholder={t('agentManagement.form.personaPlaceholder')}
                        aria-label={t('agentManagement.form.personaLabel')}
                        onBlur={() => setPersonaEditing(false)}
                        autoFocus
                        data-testid="agent-editor-persona-input"
                      />
                    ) : (
                      <div
                        className="ui-textarea-scrollable whitespace-pre-wrap break-words cursor-text text-[14px] tracking-[0]"
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
                        data-testid="agent-editor-persona-preview"
                      >
                        {draft.persona.trim() ? (
                          <div className="agent-management-markdown">
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>{draft.persona}</ReactMarkdown>
                          </div>
                        ) : (
                          <span className="text-[color:var(--color-text-placeholder)]">
                            {t('agentManagement.form.personaPlaceholder')}
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </div>
              <FieldError className="mt-1" testId="agent-editor-persona-error">
                {touched && errors.persona ? errors.persona : null}
              </FieldError>
            </div>
          </Section>

          <Section
            title={t('agentManagement.form.mcpLabel')}
            action={
              <button
                type="button"
                className="flex items-center gap-1 rounded-full px-2.5 py-1 text-[13px] text-text hover:text-[color:var(--color-chat-accent)]"
                onClick={openMcpDialog}
                data-testid="agent-editor-add-mcp"
              >
                <Plus size={14} />
                {t('agentManagement.form.addMcp')}
              </button>
            }
            collapsible
            defaultOpen={mcpOpen}
            onToggle={() => setMcpOpen((open) => !open)}
          >
            {selectedMcps.length > 0 ? (
              <div className="card-grid-auto">
                {selectedMcps.map((mcp) => (
                  <PageCard
                    key={mcp.id}
                    testId="agent-editor-mcp-item"
                    variant={mcp.id}
                    avatar={{ name: mcp.name, iconUrl: mcp.icon || undefined }}
                    title={mcp.name}
                    description={mcp.description}
                    actionsHover
                    action={{
                      icon: <DeleteCardIcon />,
                      onClick: () => update({ mcpRefs: draft.mcpRefs.filter((id) => id !== mcp.id) }),
                    }}
                  />
                ))}
              </div>
            ) : null}
          </Section>

          <Section
            title={t('agentManagement.form.skillsLabel')}
            action={
              <button
                type="button"
                className="flex items-center gap-1 rounded-full px-2.5 py-1 text-[13px] text-text hover:text-[color:var(--color-chat-accent)]"
                onClick={openSkillDialog}
                data-testid="agent-editor-add-skill"
              >
                <Plus size={14} />
                {t('agentManagement.form.addSkill')}
              </button>
            }
            collapsible
            defaultOpen={skillsOpen}
            onToggle={() => setSkillsOpen((open) => !open)}
          >
            {skillsStatus === 'loading' ? (
              <p className="py-4 text-center text-[13px] text-text-muted">{t('common.loading')}</p>
            ) : skillsStatus === 'error' ? (
              <div className="agent-management-form-error">
                <span>{t('agentManagement.form.skillsError')}</span>
                <button type="button" onClick={onReloadSkills}>
                  {t('common.retry')}
                </button>
              </div>
            ) : selectedSkills.length > 0 ? (
              <div className="card-grid-auto">
                {selectedSkills.map((skill) => (
                  <PageCard
                    key={skill.id}
                    testId="agent-editor-skill-item"
                    variant={skill.id}
                    avatar={{ name: skill.name }}
                    title={skill.name}
                    description={skill.description || t('agentManagement.unknownDescription')}
                    actionsHover
                    action={{
                      icon: <DeleteCardIcon />,
                      onClick: () => update({ skillRefs: draft.skillRefs.filter((id) => id !== skill.id) }),
                    }}
                  />
                ))}
              </div>
            ) : null}
          </Section>

          <Section
            title={t('agentManagement.form.promptsLabel')}
            action={
              <button
                type="button"
                className="flex items-center gap-1 rounded-full px-2.5 py-1 text-[13px] text-text hover:text-[color:var(--color-chat-accent)]"
                onClick={addPrompt}
                data-testid="agent-editor-add-prompt"
              >
                <Plus size={14} />
                {t('agentManagement.form.addPrompt')}
              </button>
            }
            collapsible
            defaultOpen={promptsOpen}
            onToggle={() => setPromptsOpen((open) => !open)}
          >
            {draft.suggestedPrompts.length > 0 ? (
              <div className="space-y-2">
                {draft.suggestedPrompts.map((prompt, index) => (
                  <div className="flex items-center gap-2" key={index}>
                    <Input
                      value={prompt}
                      onChange={(value) => updatePrompt(index, value)}
                      placeholder={t('agentManagement.form.promptPlaceholder')}
                      data-testid="agent-editor-prompt-input"
                      data-variant={index}
                    />
                    <button
                      type="button"
                      className="shrink-0 rounded-full p-1.5 text-text-muted hover:bg-secondary hover:text-text"
                      onClick={() => removePrompt(index)}
                      aria-label={t('agentManagement.form.removePrompt')}
                      data-testid="agent-editor-remove-prompt"
                      data-variant={index}
                    >
                      <Minus size={14} aria-hidden="true" />
                    </button>
                  </div>
                ))}
              </div>
            ) : null}
          </Section>
        </form>
      </FormPageLayout>

      {skillDialogOpen && (
        <FormDrawer
          title={t('agentManagement.form.selectSkill')}
          onClose={() => setSkillDialogOpen(false)}
          onConfirm={() => {
            update({ skillRefs: skillDraft });
            setSkillDialogOpen(false);
          }}
          testId="agent-editor-skill-picker"
          width={900}
          bodyClassName="form-drawer__body--flush"
          footerLeading={
            <SelectedCount count={skillDraft.length} testId="agent-editor-skill-picker-selected-count" />
          }
        >
          <div className="relative mx-6 mb-4 shrink-0">
            <SearchIcon
              className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[color:var(--color-text-placeholder)]"
              aria-hidden="true"
            />
            <input
              value={skillQuery}
              onChange={(event) => setSkillQuery(event.target.value)}
              placeholder={t('agentManagement.form.selectionSearchPlaceholder')}
              className="h-8 w-full rounded-lg border border-border bg-bg pl-8 pr-3 text-[12px] leading-[18px] text-text outline-none focus:border-border-hover"
              data-testid="agent-editor-skill-picker-search"
            />
          </div>
          <Tabs
            className="mb-4 px-6"
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
          <div className="form-drawer__scroll-area" ref={skillListScrollRef} onScroll={handleSkillListScroll}>
            {skillsStatus === 'loading' ? (
              <p className="py-10 text-center text-[13px] text-text-muted">{t('common.loading')}</p>
            ) : skillsStatus === 'error' ? (
              <div className="agent-management-form-error">
                <span>{t('agentManagement.form.skillsError')}</span>
                <button type="button" onClick={onReloadSkills}>
                  {t('common.retry')}
                </button>
              </div>
            ) : skillsStatus === 'success' && filteredSkills.length === 0 ? (
              <div className="py-10 text-center text-[13px] text-text-muted">
                <p>{t('agentManagement.form.skillsEmpty')}</p>
              </div>
            ) : skillsStatus === 'success' && filteredSkills.length > 0 ? (
              <>
                <div className="grid grid-cols-2 gap-4" data-testid="agent-editor-skill-picker-list">
                  {visibleSkills.map((skill) => {
                    const selected = skillDraft.includes(skill.id);
                    const installed = skill.installed === true;
                    const installing = installingSkillId === skill.id;
                    const defaultButton: PageCardDefaultButton | undefined =
                      !installed && onInstallSkill
                        ? {
                            text: installing
                              ? t('agentManagement.form.installingSkill')
                              : t('agentManagement.form.installSkill'),
                            testId: 'agent-editor-skill-picker-install',
                            variant: skill.id,
                            disabled: installing,
                            busy: installing,
                            onClick: () => void onInstallSkill(skill),
                          }
                        : undefined;
                    return (
                      <PageCard
                        key={skill.id}
                        testId="agent-editor-skill-picker-item"
                        variant={skill.id}
                        interactive={installed}
                        selected={selected}
                        disabled={!installed && !onInstallSkill}
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
                        defaultButton={defaultButton}
                        actionSlot={
                          !installed && onInstallSkill ? null : (
                            <span className="shrink-0" aria-hidden="true">
                              {selected ? (
                                <EntityRemoveIcon className="text-[color:var(--color-chat-accent)]" />
                              ) : (
                                <EntityAddIcon className="text-text-muted" />
                              )}
                            </span>
                          )
                        }
                      />
                    );
                  })}
                </div>
                {skillHasMore ? (
                  <div
                    className="flex items-center justify-center gap-2 py-4"
                    role="status"
                    aria-label={t('agentManagement.loadMore')}
                    data-testid="agent-editor-skill-picker-load-more"
                  >
                    <LoadingSpinner size={16} testId="agent-editor-skill-picker-load-more-spinner" />
                    <span className="text-sm text-text-muted">{t('agentManagement.loadMore')}</span>
                  </div>
                ) : null}
              </>
            ) : null}
          </div>
        </FormDrawer>
      )}

      {mcpDialogOpen && (
        <FormDrawer
          title={t('agentManagement.form.selectMcp')}
          onClose={() => setMcpDialogOpen(false)}
          onConfirm={() => {
            update({ mcpRefs: mcpDraft });
            setMcpDialogOpen(false);
          }}
          testId="agent-editor-mcp-picker"
          width={900}
          bodyClassName="form-drawer__body--flush"
          footerLeading={
            <SelectedCount count={mcpDraft.length} testId="agent-editor-mcp-picker-selected-count" />
          }
        >
          <Tabs
            className="mb-4 px-6"
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
          <div className="relative mx-6 mb-4 shrink-0">
            <SearchIcon
              className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[color:var(--color-text-placeholder)]"
              aria-hidden="true"
            />
            <input
              value={mcpQuery}
              onChange={(event) => setMcpQuery(event.target.value)}
              placeholder={t('agentManagement.form.selectionSearchPlaceholder')}
              className="h-8 w-full rounded-lg border border-border bg-bg pl-8 pr-3 text-[12px] leading-[18px] text-text outline-none focus:border-border-hover"
              data-testid="agent-editor-mcp-picker-search"
            />
          </div>
          {mcpStatus === 'loading' ? (
            <p className="py-10 text-center text-[13px] text-text-muted">{t('common.loading')}</p>
          ) : mcpStatus === 'error' ? (
            <div className="agent-management-form-error mx-6">
              <span>{t('agentManagement.form.mcpError')}</span>
              <button type="button" onClick={onReloadMcps}>
                {t('common.retry')}
              </button>
            </div>
          ) : mcpStatus === 'success' && filteredMcps.length === 0 ? (
            <div className="py-10 text-center text-[13px] text-text-muted">
              <p>{t('agentManagement.form.mcpEmpty')}</p>
            </div>
          ) : mcpStatus === 'success' && filteredMcps.length > 0 ? (
            <div
              className="form-drawer__scroll-area grid content-start grid-cols-2 gap-4"
              data-testid="agent-editor-mcp-picker-list"
            >
              {filteredMcps.map((mcp) => {
                const selected = mcpDraft.includes(mcp.id);
                const installed = mcp.installed === true;
                const selectable = isMcpSelectable(mcp);
                const unconnected = installed && !selectable;
                const connecting = connectingMcpId === mcp.id || mcp.connectionState === 'connecting';
                const installing = installingMcpId === mcp.id;
                const defaultButton: PageCardDefaultButton | undefined =
                  !installed && onInstallMcp
                    ? {
                        text: installing
                          ? t('agentManagement.form.installingConnector')
                          : t('agentManagement.form.installConnector'),
                        testId: 'agent-editor-mcp-picker-install',
                        variant: mcp.id,
                        disabled: installing,
                        busy: installing,
                        onClick: () => void onInstallMcp(mcp),
                      }
                    : unconnected && onConnectMcp
                      ? {
                          text: connecting
                            ? t('agentManagement.form.connectingConnector')
                            : t('agentManagement.form.connectConnector'),
                          testId: 'agent-editor-mcp-picker-connect',
                          variant: mcp.id,
                          disabled: connecting,
                          busy: connecting,
                          onClick: () => onConnectMcp(mcp),
                        }
                      : undefined;
                return (
                  <PageCard
                    key={mcp.id}
                    testId="agent-editor-mcp-picker-item"
                    variant={mcp.id}
                    interactive={selectable}
                    selected={selected}
                    disabled={!selectable && !onInstallMcp && !onConnectMcp}
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
                    defaultButton={defaultButton}
                    actionSlot={
                      !installed && onInstallMcp ? null : unconnected && onConnectMcp ? null : (
                        <span className="shrink-0" aria-hidden="true">
                          {selected ? (
                            <EntityRemoveIcon className="text-[color:var(--color-chat-accent)]" />
                          ) : (
                            <EntityAddIcon className="text-text-muted" />
                          )}
                        </span>
                      )
                    }
                  />
                );
              })}
            </div>
          ) : null}
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
