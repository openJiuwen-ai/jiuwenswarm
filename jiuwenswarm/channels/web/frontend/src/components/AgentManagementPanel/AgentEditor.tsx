import { ChevronDown, ChevronUp, Minus, Plus } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import EntityAddIcon from '../../assets/agent-management/add.svg?react';
import EntityRemoveIcon from '../../assets/agent-management/remove.svg?react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  isMcpSelectable,
  sortMcpOptions,
  type AgentDraft,
  type McpOption,
  type RequestStatus,
  type SkillOption,
} from '../../features/agentManagement';
import { AGENT_DESCRIPTION_MAX_LENGTH, AGENT_NAME_MAX_LENGTH } from '../../features/agentManagement/limits';
import { AgentTagPicker } from './AgentTagPicker';
import { DeleteCardIcon } from './cardActions';
import { SkillPickerDrawer } from './SkillPickerDrawer';
import { PageCard, Tabs, Input, Textarea, FieldError } from '../ui';
import type { PageCardDefaultButton } from '../ui';
import { ConnectorPickerDrawer } from '../ConnectorMarket/ConnectorPickerDrawer';
import { FormPageLayout } from '../ConnectorMarket/FormPageLayout';

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
  const sortedMcps = useMemo(() => sortMcpOptions(mcpOptions), [mcpOptions]);
  // MCP 选择抽屉的页签与过滤需保持稳定引用：ConnectorPickerDrawer 的 visibleItems useMemo
  // 依赖 tabs/filterItem，内联字面量会让父组件每次渲染（如点安装/连接触发 busy 态）都生成
  // 新数组，把 PickerListRegion 已触底加载的列表打回首屏。
  const mcpPickerTabs = useMemo(
    () => ({
      ariaLabel: t('agentManagement.form.mcpSourceTabsLabel'),
      items: [
        { value: 'market' as const, label: t('agentManagement.form.mcpMarket') },
        { value: 'installed' as const, label: t('agentManagement.form.myMcp') },
      ],
    }),
    [t],
  );
  const filterMcpBySourceTab = useCallback((mcp: McpOption, sourceTab: 'market' | 'installed') => {
    const isMarketplace = mcp.source === 'built_in' || mcp.source === 'hub';
    // "我的" = 自定义 + 已连接的（预置/hub）；installed 只表达安装态（预置恒 true），
    // 不能单独作为 tab 归属，否则未连接预置会同时出现在两个 tab。
    const isMine = mcp.source === 'customize' || (mcp.installed === true && mcp.connectionState === 'connected');
    return sourceTab === 'market' ? isMarketplace : isMine;
  }, []);

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
                          // 仅在预览自身聚焦时劫持 Enter/Space；markdown 内链接聚焦时按 Enter 应正常打开链接
                          if (event.target !== event.currentTarget) return;
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
                onClick={() => setMcpDialogOpen(true)}
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
                onClick={() => setSkillDialogOpen(true)}
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
        <SkillPickerDrawer
          title={t('agentManagement.form.selectSkill')}
          testId="agent-editor-skill-picker"
          status={skillsStatus}
          skills={skillOptions}
          initialSelectedIds={draft.skillRefs}
          onClose={() => setSkillDialogOpen(false)}
          onConfirm={(ids) => {
            update({ skillRefs: ids });
            setSkillDialogOpen(false);
          }}
          onRetry={onReloadSkills}
          onInstallSkill={onInstallSkill}
          installingSkillId={installingSkillId}
        />
      )}

      {mcpDialogOpen && (
        <ConnectorPickerDrawer
          title={t('agentManagement.form.selectMcp')}
          testId="agent-editor-mcp-picker"
          status={mcpStatus}
          items={sortedMcps}
          getItemKey={(mcp) => mcp.id}
          initialSelectedIds={draft.mcpRefs}
          onClose={() => setMcpDialogOpen(false)}
          onConfirm={(ids) => {
            update({ mcpRefs: ids });
            setMcpDialogOpen(false);
          }}
          onRetry={onReloadMcps}
          tabs={mcpPickerTabs}
          filterItem={filterMcpBySourceTab}
          errorMessage={t('agentManagement.form.mcpError')}
          emptyMessage={t('agentManagement.form.mcpEmpty')}
          loadMoreLabel={t('agentManagement.loadMore')}
          searchPlaceholder={t('agentManagement.form.selectionSearchPlaceholder')}
          fallbackDescription={t('agentManagement.unknownDescription')}
          renderItem={(mcp, { selected, toggle }) => {
            const installed = mcp.installed === true;
            const preset = mcp.source === 'built_in';
            const selectable = isMcpSelectable(mcp);
            const connectable = (installed || preset) && !selectable;
            const connecting = connectingMcpId === mcp.id || mcp.connectionState === 'connecting';
            const installing = installingMcpId === mcp.id;
            const defaultButton: PageCardDefaultButton | undefined =
              !installed && !preset && onInstallMcp
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
                : connectable && onConnectMcp
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
                testId="agent-editor-mcp-picker-item"
                variant={mcp.id}
                interactive={selectable}
                selected={selected}
                disabled={!selectable && !onInstallMcp && !onConnectMcp}
                onClick={selectable ? () => toggle(mcp.id) : undefined}
                avatar={{ name: mcp.name, iconUrl: mcp.icon || undefined }}
                title={mcp.name}
                description={mcp.description || t('agentManagement.unknownDescription')}
                defaultButton={defaultButton}
                actionSlot={
                  defaultButton ? null : (
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
          }}
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
