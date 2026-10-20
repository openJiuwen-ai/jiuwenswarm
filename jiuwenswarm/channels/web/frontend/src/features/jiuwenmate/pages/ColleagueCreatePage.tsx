import { ArrowRightLeft, Check, ChevronDown, Plus, Trash2, X } from 'lucide-react';
import { useRef, useState, type ChangeEvent } from 'react';
import { useTranslation } from 'react-i18next';
import sparkleIcon from '../../../assets/jiuwenmate/icons/sparkle.svg';
import { ActionMenu } from '../components/ActionMenu';
import { CapabilitySection, CollapsibleSection, type CapabilityKind } from '../components/CapabilitySection';
import { ColleagueCard } from '../components/ColleagueCard';
import { CustomCreateDialog } from '../components/CustomCreateDialog';
import { JiuwenMateDialog } from '../components/JiuwenMateDialog';
import { TemplateDetailDrawer } from '../components/TemplateDetailDrawer';
import { colleaguePortrait, customAvatarHue } from '../colleaguePortraits';
import type { JiuwenMateModelCatalog } from '../jiuwenmateApi';
import {
  selectGeneratedColleagueCapabilities,
  type ColleagueCapabilityCatalog,
  type MutableColleagueCapabilitySelections,
  type SkillInstallCandidate,
  type TemplateSkillRecommendations,
} from '../colleagueCapabilities';
import { ModelLabel } from '../components/ModelLabel';
import {
  JIUWENMATE_GENERATED_CAPABILITIES,
  JIUWENMATE_TEMPLATES,
  JIUWENMATE_WORK_DOMAINS,
} from '../mockData';
import type {
  JiuwenMateCapabilities,
  JiuwenMateColleagueDraft,
  JiuwenMateCreationSource,
  JiuwenMateCreationStep,
  JiuwenMateTemplate,
} from '../types';

const DESCRIPTION_LIMIT = 200;
const AVATAR_SIZE_LIMIT = 200 * 1024;
const AVATAR_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/jpg'];
const CAPABILITY_KINDS: readonly CapabilityKind[] = ['skills', 'connectors', 'plugins', 'knowledge'];

export interface ColleagueCreatePageProps {
  step: JiuwenMateCreationStep;
  source: JiuwenMateCreationSource;
  detailTemplateId: string | null;
  customDialogOpen: boolean;
  usedAvatarHues?: readonly number[];
  draft: JiuwenMateColleagueDraft | null;
  modelCatalog: JiuwenMateModelCatalog;
  resourceCatalog: ColleagueCapabilityCatalog;
  resourceCatalogReady: boolean;
  missingTemplateCapabilities: MutableColleagueCapabilitySelections;
  skillRecommendations: TemplateSkillRecommendations;
  onDismissSkillRecommendationError: () => void;
  onInstallSkill: (skill: SkillInstallCandidate) => Promise<string>;
  submitting: boolean;
  onOpenTemplateDetail: (id: string) => void;
  onCloseTemplateDetail: () => void;
  onChooseTemplate: (template: JiuwenMateTemplate) => void;
  onOpenCustom: () => void;
  onCloseCustom: () => void;
  onConfirmCustom: (basics: { name: string; description: string; avatarHue: number }) => void;
  onOpenPlaza: () => void;
  onDraftChange: (patch: Partial<JiuwenMateColleagueDraft>) => void;
  onCancel: () => void;
  onComplete: () => void;
}

export function ColleagueCreatePage(props: ColleagueCreatePageProps) {
  const { t } = useTranslation();
  const { step, source, draft } = props;
  const detailTemplate = JIUWENMATE_TEMPLATES.find((item) => item.id === props.detailTemplateId) ?? null;

  if (step === 'refine' && draft) {
    return (
      <ColleagueRefineForm
        source={source}
        draft={draft}
        modelCatalog={props.modelCatalog}
        resourceCatalog={props.resourceCatalog}
        resourceCatalogReady={props.resourceCatalogReady}
        missingTemplateCapabilities={props.missingTemplateCapabilities}
        skillRecommendations={props.skillRecommendations}
        onDismissSkillRecommendationError={props.onDismissSkillRecommendationError}
        onInstallSkill={props.onInstallSkill}
        submitting={props.submitting}
        onDraftChange={props.onDraftChange}
        onCancel={props.onCancel}
        onComplete={props.onComplete}
      />
    );
  }

  return (
    <section className="jiuwenmate-create" data-testid="jiuwenmate-create-page" data-variant="select">
      <p className="jiuwenmate-breadcrumb">
        <span>{t('jiuwenmate.nav.office')}</span>
        <span aria-hidden>/</span>
        <strong>{t('jiuwenmate.create.crumb')}</strong>
      </p>
      <header className="jiuwenmate-page-header jiuwenmate-page-header--compact">
        <h1>{t('jiuwenmate.create.title')}</h1>
        <button
          type="button"
          className="jiuwenmate-dark-button"
          onClick={props.onOpenCustom}
          data-testid="jiuwenmate-custom-create-entry"
        >
          {t('jiuwenmate.create.customEntry')}
        </button>
      </header>
      <div className="jiuwenmate-create__select" data-testid="jiuwenmate-template-selection">
        {JIUWENMATE_TEMPLATES.map((template) => (
          <ColleagueCard
            key={template.id}
            colleague={template}
            onSelect={() => props.onOpenTemplateDetail(template.id)}
            actions={[
              { label: t('jiuwenmate.create.viewDetail'), onClick: () => props.onOpenTemplateDetail(template.id) },
              { label: t('jiuwenmate.create.choose'), primary: true, onClick: () => props.onChooseTemplate(template) },
            ]}
          />
        ))}
      </div>
      {detailTemplate ? (
        <TemplateDetailDrawer template={detailTemplate} onClose={props.onCloseTemplateDetail} onChoose={props.onChooseTemplate} />
      ) : null}
      {props.customDialogOpen ? (
        <CustomCreateDialog
          usedHues={props.usedAvatarHues}
          onCancel={props.onCloseCustom}
          onConfirm={props.onConfirmCustom}
          onOpenPlaza={props.onOpenPlaza}
        />
      ) : null}
    </section>
  );
}

interface ColleagueRefineFormProps {
  source: JiuwenMateCreationSource;
  draft: JiuwenMateColleagueDraft;
  modelCatalog: JiuwenMateModelCatalog;
  resourceCatalog: ColleagueCapabilityCatalog;
  resourceCatalogReady: boolean;
  missingTemplateCapabilities: MutableColleagueCapabilitySelections;
  skillRecommendations: TemplateSkillRecommendations;
  onDismissSkillRecommendationError: () => void;
  onInstallSkill: (skill: SkillInstallCandidate) => Promise<string>;
  submitting: boolean;
  onDraftChange: (patch: Partial<JiuwenMateColleagueDraft>) => void;
  onCancel: () => void;
  onComplete: () => void;
}

function ColleagueRefineForm({
  source,
  draft,
  modelCatalog,
  resourceCatalog,
  resourceCatalogReady,
  missingTemplateCapabilities,
  skillRecommendations,
  onDismissSkillRecommendationError,
  onInstallSkill,
  submitting,
  onDraftChange,
  onCancel,
  onComplete,
}: ColleagueRefineFormProps) {
  const { t } = useTranslation();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [avatarError, setAvatarError] = useState<string | null>(null);
  const [installingSkill, setInstallingSkill] = useState<string | null>(null);
  const [skillInstallError, setSkillInstallError] = useState<string | null>(null);
  const canSubmit = !submitting && draft.name.trim().length > 0 && draft.description.trim().length > 0;
  const hue = customAvatarHue(draft);
  const updateCapabilities = (patch: Partial<JiuwenMateCapabilities>) =>
    onDraftChange({ capabilities: { ...draft.capabilities, ...patch } });

  const handleAvatarFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    if (!AVATAR_TYPES.includes(file.type) || file.size > AVATAR_SIZE_LIMIT) {
      setAvatarError(t('jiuwenmate.create.avatarInvalid'));
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      setAvatarError(null);
      onDraftChange({ avatarUrl: String(reader.result) });
    };
    reader.readAsDataURL(file);
  };

  const generateCapabilities = () => {
    const merge = (current: readonly string[], extra: readonly string[]) => [...new Set([...current, ...extra])];
    const generated = selectGeneratedColleagueCapabilities(JIUWENMATE_GENERATED_CAPABILITIES, resourceCatalog);
    updateCapabilities({
      skills: merge(draft.capabilities.skills, generated.skills),
      connectors: merge(draft.capabilities.connectors, generated.connectors),
      plugins: merge(draft.capabilities.plugins, generated.plugins),
      knowledge: merge(draft.capabilities.knowledge, generated.knowledge),
    });
  };

  const installRecommendedSkill = async (skill: SkillInstallCandidate) => {
    setInstallingSkill(skill.identifier);
    setSkillInstallError(null);
    try {
      const installedName = await onInstallSkill(skill);
      updateCapabilities({ skills: [...new Set([...draft.capabilities.skills, installedName])] });
    } catch (error) {
      setSkillInstallError(error instanceof Error ? error.message : t('jiuwenmate.create.skillInstallFailed'));
    } finally {
      setInstallingSkill(null);
    }
  };

  const addInstalledSkill = (name: string) => {
    if (draft.capabilities.skills.includes(name)) return;
    updateCapabilities({ skills: [...draft.capabilities.skills, name] });
  };

  const closeSkillError = () => {
    setSkillInstallError(null);
    if (!skillInstallError && skillRecommendations.error) onDismissSkillRecommendationError();
  };

  const skillDialogError = skillInstallError ?? skillRecommendations.error;

  const skillSourceLabel = (sourceName: string) => {
    if (sourceName === 'teamskillshub') return 'SwarmSkillHub';
    if (sourceName === 'clawhub') return 'ClawHub';
    if (sourceName === 'skillnet') return 'SkillNet';
    return sourceName;
  };

  const cycleModel = () => {
    const { models, defaultModelId } = modelCatalog;
    if (models.length === 0) return;
    const index = models.findIndex((item) => item.id === (draft.capabilities.model || defaultModelId));
    updateCapabilities({ model: models[(index + 1) % models.length].id });
  };

  return (
    <section className="jiuwenmate-create jiuwenmate-create--refine" data-testid="jiuwenmate-create-page" data-variant={source}>
      <div className="jiuwenmate-create__scroll">
        <p className="jiuwenmate-breadcrumb">
          <span>{t('jiuwenmate.nav.office')}</span>
          <span aria-hidden>/</span>
          <span>{t('jiuwenmate.create.crumb')}</span>
          <span aria-hidden>/</span>
          <strong>{source === 'custom' ? t('jiuwenmate.create.customTitle') : t('jiuwenmate.create.refineCrumb')}</strong>
        </p>
        <form
          className="jiuwenmate-create__form"
          id="jiuwenmate-refine-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (canSubmit) onComplete();
          }}
          data-testid="jiuwenmate-custom-create-form"
        >
          <div className="jiuwenmate-create__basic">
            <h2>{t('jiuwenmate.create.basicInfo')}</h2>
            <div className="jiuwenmate-avatar-row">
              <button
                type="button"
                className="jiuwenmate-avatar jiuwenmate-avatar--medium"
                aria-label={t('jiuwenmate.create.changeAvatar')}
                data-hue={hue ?? undefined}
                style={hue === null ? undefined : { ['--jm-avatar-hue' as string]: `${hue}deg` }}
                onClick={() => fileInputRef.current?.click()}
              >
                <img src={colleaguePortrait(draft)} alt="" />
              </button>
              <button
                type="button"
                className="jiuwenmate-avatar-row__generate"
                aria-label={t('jiuwenmate.create.resetAvatar')}
                onClick={() => {
                  setAvatarError(null);
                  onDraftChange({ avatarUrl: undefined });
                }}
              >
                <img src={sparkleIcon} alt="" aria-hidden />
              </button>
              <small data-error={avatarError ? 'true' : undefined}>{avatarError ?? t('jiuwenmate.create.avatarHint')}</small>
              <input ref={fileInputRef} type="file" accept=".png,.jpeg,.jpg,.gif" hidden onChange={handleAvatarFile} />
            </div>
            <label className="jiuwenmate-field">
              <span>{t('jiuwenmate.create.name')}</span>
              <input
                value={draft.name}
                onChange={(event) => onDraftChange({ name: event.target.value })}
                placeholder={t('jiuwenmate.create.namePlaceholder')}
                data-testid="jiuwenmate-colleague-name-input"
              />
            </label>
            <label className="jiuwenmate-field">
              <span>{t('jiuwenmate.create.description')}</span>
              <span className="jiuwenmate-field__textarea">
                <textarea
                  value={draft.description}
                  maxLength={DESCRIPTION_LIMIT}
                  onChange={(event) => onDraftChange({ description: event.target.value })}
                  placeholder={t('jiuwenmate.create.descriptionPlaceholder')}
                  rows={3}
                  data-testid="jiuwenmate-colleague-description-input"
                />
                <small>{`${draft.description.length}/${DESCRIPTION_LIMIT}`}</small>
              </span>
            </label>
            <div className="jiuwenmate-field">
              <span>{t('jiuwenmate.create.role')}</span>
              <DomainSelect value={draft.tags} onChange={(tags) => onDraftChange({ tags })} />
            </div>
          </div>

          <aside className="jiuwenmate-create__preview" data-category={draft.category} aria-label={t('jiuwenmate.create.preview')}>
            <img
              src={colleaguePortrait(draft)}
              alt=""
              data-hue={hue ?? undefined}
              style={hue === null ? undefined : { ['--jm-avatar-hue' as string]: `${hue}deg` }}
            />
            <h3>{draft.name || t('jiuwenmate.create.previewName')}</h3>
            <p>{draft.description || t('jiuwenmate.create.previewDescription')}</p>
            <span className="jiuwenmate-colleague-card__tags">
              {draft.tags.map((tag) => (
                <em key={tag}>{tag}</em>
              ))}
            </span>
          </aside>

          <div className="jiuwenmate-create__capabilities">
            <header>
              <div>
                <h2>{t('jiuwenmate.create.capabilities')}</h2>
                <p>{t('jiuwenmate.create.capabilitiesHint')}</p>
              </div>
              <button type="button" className="jiuwenmate-outline-button jiuwenmate-outline-button--icon" onClick={generateCapabilities}>
                <img src={sparkleIcon} alt="" aria-hidden />
                {t('jiuwenmate.create.generate')}
              </button>
            </header>
            {source === 'template' && CAPABILITY_KINDS.some((kind) => missingTemplateCapabilities[kind].length > 0) ? (
              <section className="jiuwenmate-create__unavailable" data-testid="jiuwenmate-template-missing-capabilities">
                <strong>{t('jiuwenmate.create.unavailableCapabilitiesTitle')}</strong>
                <p>{t('jiuwenmate.create.unavailableCapabilitiesHint')}</p>
                {CAPABILITY_KINDS.map((kind) =>
                  missingTemplateCapabilities[kind].length > 0 ? (
                    <div key={kind}>
                      <span>{t(`jiuwenmate.create.sections.${kind}`)}:</span>
                      {missingTemplateCapabilities[kind].map((name) => {
                        return (
                          <span key={`${kind}:${name}`} className="jiuwenmate-create__unavailable-item">
                            <span className="jiuwenmate-create__unavailable-name">{name}</span>
                            <small>{t('jiuwenmate.create.capabilityNotLoaded')}</small>
                          </span>
                        );
                      })}
                    </div>
                  ) : null,
                )}
              </section>
            ) : null}
            {source === 'template' ? (
              <section
                className="jiuwenmate-create__skill-recommendations"
                data-testid="jiuwenmate-template-skill-recommendations"
              >
                <div>
                  <strong>{t('jiuwenmate.create.skillRecommendationsTitle')}</strong>
                  <p>{t('jiuwenmate.create.skillRecommendationsHint')}</p>
                </div>
                {skillRecommendations.status === 'loading' ? (
                  <p className="jiuwenmate-create__skill-recommendations-status">
                    {t('jiuwenmate.create.skillRecommendationsLoading')}
                  </p>
                ) : null}
                {skillRecommendations.local.length > 0 ? (
                  <div className="jiuwenmate-create__skill-group" data-testid="jiuwenmate-template-local-skill-recommendations">
                    <h3>{t('jiuwenmate.create.localSkillRecommendations')}</h3>
                    {skillRecommendations.local.map((skill, index) => {
                      const added = draft.capabilities.skills.includes(skill.name);
                      return (
                        <article
                          key={skill.name}
                          className="jiuwenmate-create__skill-recommendation-item"
                          data-testid={`jiuwenmate-template-local-skill-${index}`}
                        >
                          <div>
                            <strong className="jiuwenmate-create__skill-name">{skill.label}</strong>
                            {skill.description ? <p>{skill.description}</p> : null}
                          </div>
                          <button
                            type="button"
                            className="jiuwenmate-outline-button jiuwenmate-create__skill-action"
                            disabled={added}
                            onClick={() => addInstalledSkill(skill.name)}
                            data-testid={`jiuwenmate-add-recommended-local-skill-${index}`}
                          >
                            {added ? t('jiuwenmate.create.skillAdded') : t('jiuwenmate.create.addRecommendedSkill')}
                          </button>
                        </article>
                      );
                    })}
                  </div>
                ) : null}
                {skillRecommendations.plaza.length > 0 ? (
                  <div className="jiuwenmate-create__skill-group" data-testid="jiuwenmate-template-plaza-skill-recommendations">
                    <h3>{t('jiuwenmate.create.plazaSkillRecommendations')}</h3>
                    {skillRecommendations.plaza.map((skill, index) => {
                      const added = draft.capabilities.skills.includes(skill.name);
                      const isInstalling = installingSkill === skill.identifier;
                      return (
                        <article
                          key={`${skill.source}:${skill.identifier}`}
                          className="jiuwenmate-create__skill-recommendation-item"
                          data-testid={`jiuwenmate-template-plaza-skill-${index}`}
                        >
                          <div>
                            <strong className="jiuwenmate-create__skill-name">{skill.display_name}</strong>
                            <small>{skillSourceLabel(skill.source)}</small>
                            {skill.description ? <p>{skill.description}</p> : null}
                          </div>
                          <button
                            type="button"
                            className="jiuwenmate-outline-button jiuwenmate-create__skill-action jiuwenmate-create__install-button"
                            disabled={installingSkill !== null || added}
                            onClick={() => void installRecommendedSkill(skill)}
                            data-testid={`jiuwenmate-install-recommended-plaza-skill-${index}`}
                          >
                            {added
                              ? t('jiuwenmate.create.skillAdded')
                              : isInstalling
                                ? t('jiuwenmate.create.installingRecommendedSkill')
                                : t('jiuwenmate.create.installRecommendedSkill')}
                          </button>
                        </article>
                      );
                    })}
                  </div>
                ) : null}
              </section>
            ) : null}
            <CollapsibleSection title={t('jiuwenmate.create.sections.model')} testId="jiuwenmate-capability-section">
              <div className="jiuwenmate-model-card">
                <ModelLabel catalog={modelCatalog} modelId={draft.capabilities.model} />
                <button
                  type="button"
                  aria-label={t('jiuwenmate.create.switchModel')}
                  onClick={cycleModel}
                  disabled={modelCatalog.models.length < 2}
                >
                  <ArrowRightLeft aria-hidden />
                </button>
              </div>
            </CollapsibleSection>
            {CAPABILITY_KINDS.map((kind) => (
              <CapabilitySection
                key={kind}
                kind={kind}
                title={t(`jiuwenmate.create.sections.${kind}`)}
                addLabel={t(`jiuwenmate.create.add.${kind}`)}
                removeLabel={t('jiuwenmate.create.remove')}
                emptyLabel={t(`jiuwenmate.create.empty.${kind}`)}
                items={draft.capabilities[kind]}
                catalog={(resourceCatalog[kind] ?? []).map((option) => option.name)}
                emptyMenuLabel={
                  !resourceCatalogReady
                    ? t('jiuwenmate.profile.catalogLoading')
                    : resourceCatalog[kind] === null
                      ? t('jiuwenmate.profile.catalogFailed')
                      : resourceCatalog[kind]?.length === 0
                        ? t(`jiuwenmate.profile.catalogEmpty${kind[0].toUpperCase()}${kind.slice(1)}`)
                        : undefined
                }
                onChange={(items) => updateCapabilities({ [kind]: items })}
              />
            ))}
            {source === 'template' ? (
              <CollapsibleSection
                title={t('jiuwenmate.create.sections.starterPrompts')}
                action={
                  <button
                    type="button"
                    className="jiuwenmate-collapsible__add-button"
                    onClick={() => updateCapabilities({ starterPrompts: [...draft.capabilities.starterPrompts, ''] })}
                  >
                    <Plus aria-hidden />
                    <span>{t('jiuwenmate.create.add.starterPrompts')}</span>
                  </button>
                }
              >
                <div className="jiuwenmate-starter-prompts">
                  {draft.capabilities.starterPrompts.map((prompt, index) => (
                    <div key={index}>
                      <input
                        value={prompt}
                        onChange={(event) =>
                          updateCapabilities({
                            starterPrompts: draft.capabilities.starterPrompts.map((item, itemIndex) =>
                              itemIndex === index ? event.target.value : item,
                            ),
                          })
                        }
                      />
                      <ActionMenu
                        label={t('jiuwenmate.workspace.more')}
                        items={[
                          {
                            key: 'delete',
                            label: t('jiuwenmate.office.delete'),
                            icon: Trash2,
                            onSelect: () =>
                              updateCapabilities({
                                starterPrompts: draft.capabilities.starterPrompts.filter((_, itemIndex) => itemIndex !== index),
                              }),
                          },
                        ]}
                      />
                    </div>
                  ))}
                </div>
              </CollapsibleSection>
            ) : null}
          </div>
        </form>
      </div>
      <footer className="jiuwenmate-create__footer">
        <button type="button" className="jiuwenmate-outline-button" onClick={onCancel}>
          {t('jiuwenmate.common.cancel')}
        </button>
        <button
          type="submit"
          form="jiuwenmate-refine-form"
          className="jiuwenmate-dark-button"
          disabled={!canSubmit}
          data-testid="jiuwenmate-create-submit-button"
        >
          {t('jiuwenmate.common.create')}
        </button>
      </footer>
      {skillDialogError ? (
        <JiuwenMateDialog
          title={t('jiuwenmate.create.skillInstallErrorTitle')}
          onClose={closeSkillError}
          testId={
            skillInstallError
              ? 'jiuwenmate-install-skill-error-dialog'
              : 'jiuwenmate-template-skill-error-dialog'
          }
          footer={
            <button
              type="button"
              className="jiuwenmate-dark-button"
              onClick={closeSkillError}
              data-testid="jiuwenmate-install-skill-error-confirm"
            >
              {t('jiuwenmate.common.confirm')}
            </button>
          }
        >
          <p className="jiuwenmate-dialog__message">{skillDialogError}</p>
        </JiuwenMateDialog>
      ) : null}
    </section>
  );
}

function DomainSelect({ value, onChange }: { value: readonly string[]; onChange: (value: string[]) => void }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const toggle = (domain: string) =>
    onChange(value.includes(domain) ? value.filter((item) => item !== domain) : [...value, domain]);

  return (
    <div className="jiuwenmate-domain-select" data-open={open} onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
    }}>
      <div className="jiuwenmate-domain-select__control">
        {value.map((domain) => (
          <span key={domain} className="jiuwenmate-domain-select__chip">
            {domain}
            <button type="button" aria-label={`${t('jiuwenmate.create.remove')} ${domain}`} onClick={() => toggle(domain)}>
              <X aria-hidden />
            </button>
          </span>
        ))}
        <button
          type="button"
          className="jiuwenmate-domain-select__toggle"
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-label={t('jiuwenmate.create.role')}
          onClick={() => setOpen((current) => !current)}
          data-testid="jiuwenmate-colleague-role-input"
        >
          <ChevronDown aria-hidden />
        </button>
      </div>
      {open ? (
        <div className="jiuwenmate-domain-select__list" role="listbox" aria-multiselectable="true">
          {JIUWENMATE_WORK_DOMAINS.map((domain) => (
            <button key={domain} type="button" role="option" aria-selected={value.includes(domain)} onClick={() => toggle(domain)}>
              <span>{domain}</span>
              {value.includes(domain) ? <Check aria-hidden /> : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

