import {
  CalendarDays,
  ChartLine,
  ChevronDown,
  CirclePause,
  CirclePlay,
  HardDrive,
  Monitor,
  PencilLine,
  Plus,
  Search,
  SquareArrowOutUpRight,
  Trash2,
  UsersRound,
  X,
  type LucideIcon,
} from 'lucide-react';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import feishuIcon from '../../../assets/jiuwenmate/icons/feishu.svg';
import scheduleEmpty from '../../../assets/jiuwenmate/illustrations/schedule-empty.png';
import { ActionMenu } from '../components/ActionMenu';
import { ActivityHeatmap, HeatmapLegend } from '../components/ActivityHeatmap';
import { ColleagueAvatar } from '../components/ColleagueAvatar';
import { ModelLabel } from '../components/ModelLabel';
import type { ColleagueResourceOption, JiuwenMateModelCatalog } from '../jiuwenmateApi';
import { JIUWENMATE_WORK_DOMAINS } from '../mockData';
import { halfYearHeatmap } from '../officeState';
import type { JiuwenMateColleague } from '../types';

export type JiuwenMateProfileSection =
  | 'overview'
  | 'persona'
  | 'model'
  | 'skills'
  | 'connectors'
  | 'plugins'
  | 'knowledge'
  | 'channels'
  | 'schedules';

export const PROFILE_SECTIONS: readonly JiuwenMateProfileSection[] = [
  'overview',
  'persona',
  'model',
  'skills',
  'connectors',
  'plugins',
  'knowledge',
  'channels',
  'schedules',
];

type WorkLogPeriod = '7d' | '1m' | '3m' | 'all';
const WORK_LOG_PERIODS: readonly WorkLogPeriod[] = ['7d', '1m', '3m', 'all'];
const EMPTY_MODEL_CATALOG: JiuwenMateModelCatalog = { models: [], defaultModelId: '', defaultModelLabel: '' };
const DAY_MS = 24 * 60 * 60 * 1000;

export interface ColleagueResourceCatalog {
  skills: readonly ColleagueResourceOption[] | null;
  connectors: readonly ColleagueResourceOption[] | null;
  plugins: readonly ColleagueResourceOption[] | null;
  ready: boolean;
}

const RESOURCE_SECTIONS = ['skills', 'connectors', 'plugins'] as const;
type ResourceSection = (typeof RESOURCE_SECTIONS)[number];
const PENCIL_SECTIONS = ['overview', 'persona', 'model'] as const;

export interface ColleagueSettingsEdit {
  name?: string;
  description?: string;
  tags?: string[];
  model?: string;
  skills?: string[];
  connectors?: string[];
  plugins?: string[];
}

interface ColleagueProfilePageProps {
  colleague: JiuwenMateColleague;
  modelCatalog?: JiuwenMateModelCatalog;
  initialSection?: JiuwenMateProfileSection;
  today?: Date;
  resourceCatalog?: ColleagueResourceCatalog;
  savingResources?: boolean;
  initialEditing?: boolean;
  onSaveSettings?: (edit: ColleagueSettingsEdit) => void | Promise<void>;
  onOpenOffice: () => void;
  onChat: () => void;
  onConfigureIm: () => void;
  onToggleDisabled: () => void;
  onDelete: () => void;
}

export function formatJoinedAt(joinedAt: string): string {
  const date = new Date(joinedAt);
  if (Number.isNaN(date.getTime())) return joinedAt;
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function joinedDays(joinedAt: string, today: Date): number {
  const joined = new Date(joinedAt);
  if (Number.isNaN(joined.getTime())) return 1;
  const startOfJoined = new Date(joined.getFullYear(), joined.getMonth(), joined.getDate());
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  return Math.max(1, Math.round((startOfToday.getTime() - startOfJoined.getTime()) / DAY_MS) + 1);
}

export function ColleagueProfilePage({
  colleague,
  modelCatalog = EMPTY_MODEL_CATALOG,
  initialSection = 'overview',
  today = new Date(),
  resourceCatalog,
  savingResources = false,
  initialEditing = false,
  onSaveSettings,
  onOpenOffice,
  onChat,
  onConfigureIm,
  onToggleDisabled,
  onDelete,
}: ColleagueProfilePageProps) {
  const { t } = useTranslation();
  const [section, setSection] = useState<JiuwenMateProfileSection>(initialSection);
  const [editing, setEditing] = useState(initialEditing);
  const [name, setName] = useState(colleague.name);
  const [description, setDescription] = useState(colleague.description);
  const [tags, setTags] = useState(colleague.tags);
  const [model, setModel] = useState(colleague.capabilities.model);
  const [skills, setSkills] = useState(colleague.capabilities.skills);
  const [connectors, setConnectors] = useState(colleague.capabilities.connectors);
  const [plugins, setPlugins] = useState(colleague.capabilities.plugins);
  const showPencil = PENCIL_SECTIONS.some((item) => item === section);
  const saveSettings = (edit: ColleagueSettingsEdit) => {
    void Promise.resolve(onSaveSettings?.(edit))
      .then(() => setEditing(false))
      .catch(() => undefined);
  };
  const openSection = (next: JiuwenMateProfileSection) => {
    setSection(next);
    setEditing(false);
  };

  return (
    <section className="jiuwenmate-page jiuwenmate-profile" data-testid="jiuwenmate-colleague-profile">
      <nav className="jiuwenmate-breadcrumb" aria-label={t('jiuwenmate.create.crumbLabel')}>
        <button type="button" onClick={onOpenOffice}>
          {t('jiuwenmate.nav.office')}
        </button>
        <span aria-hidden>/</span>
        <strong>{colleague.name}</strong>
      </nav>
      <div className="jiuwenmate-profile__frame">
        <nav className="jiuwenmate-profile__subnav" aria-label={t('jiuwenmate.profile.sectionsLabel')}>
          {PROFILE_SECTIONS.map((item) => (
            <button
              key={item}
              type="button"
              aria-current={section === item ? 'true' : undefined}
              onClick={() => openSection(item)}
              data-testid="jiuwenmate-profile-section"
              data-variant={item}
            >
              {t(`jiuwenmate.profile.sections.${item}`)}
            </button>
          ))}
        </nav>
        <div className="jiuwenmate-profile__content">
          <header className="jiuwenmate-profile__header">
            <ColleagueAvatar colleague={colleague} size="large" />
            <h1>{colleague.name}</h1>
            {showPencil ? (
              <button
                type="button"
                className="jiuwenmate-icon-button"
                aria-label={t(section === 'model' ? 'jiuwenmate.profile.editModel' : 'jiuwenmate.profile.edit')}
                aria-pressed={editing}
                onClick={() => setEditing((current) => !current)}
                data-testid="jiuwenmate-profile-edit"
              >
                <PencilLine aria-hidden />
              </button>
            ) : null}
            {colleague.disabled ? <em className="jiuwenmate-badge" data-tone="muted">{t('jiuwenmate.office.disabledBadge')}</em> : null}
            <span className="jiuwenmate-profile__header-actions">
              <ActionMenu
                label={t('jiuwenmate.workspace.more')}
                items={[
                  {
                    key: 'toggle',
                    label: colleague.disabled ? t('jiuwenmate.office.enable') : t('jiuwenmate.office.disable'),
                    icon: colleague.disabled ? CirclePlay : CirclePause,
                    onSelect: onToggleDisabled,
                  },
                  { key: 'delete', label: t('jiuwenmate.office.delete'), icon: Trash2, onSelect: onDelete },
                ]}
              />
              <button type="button" className="jiuwenmate-dark-button" onClick={onChat} data-testid="jiuwenmate-profile-chat">
                {t('jiuwenmate.office.chat')}
              </button>
            </span>
          </header>
          {section === 'overview' ? (
            <>
              {editing ? (
                <IdentityEditor
                  name={name}
                  description={description}
                  tags={tags}
                  saving={savingResources}
                  onNameChange={setName}
                  onDescriptionChange={setDescription}
                  onTagsChange={setTags}
                  onSave={() => saveSettings({ name: name.trim(), description: description.trim(), tags: [...tags] })}
                />
              ) : null}
              <ProfileOverview colleague={colleague} today={today} onConfigureIm={onConfigureIm} />
            </>
          ) : (
            <ProfileSectionDetail
              colleague={colleague}
              modelCatalog={modelCatalog}
              section={section}
              editing={editing}
              name={name}
              description={description}
              tags={tags}
              model={model}
              skills={skills}
              connectors={connectors}
              plugins={plugins}
              resourceCatalog={resourceCatalog}
              savingResources={savingResources}
              onNameChange={setName}
              onDescriptionChange={setDescription}
              onTagsChange={setTags}
              onModelChange={setModel}
              onSkillsChange={setSkills}
              onConnectorsChange={setConnectors}
              onPluginsChange={setPlugins}
              onSaveSettings={saveSettings}
              onConfigureIm={onConfigureIm}
            />
          )}
        </div>
      </div>
    </section>
  );
}

function ProfileOverview({
  colleague,
  today,
  onConfigureIm,
}: {
  colleague: JiuwenMateColleague;
  today: Date;
  onConfigureIm: () => void;
}) {
  const { t } = useTranslation();
  const [period, setPeriod] = useState<WorkLogPeriod>('7d');
  const [half, setHalf] = useState<1 | 2>(today.getMonth() < 6 ? 1 : 2);
  const [hoveredCellId, setHoveredCellId] = useState<string | null>(null);
  const year = today.getFullYear();
  const heatmap = useMemo(() => halfYearHeatmap(year, half, []), [year, half]);

  const metrics: { key: string; value: number; icon: LucideIcon }[] = [
    { key: 'weeklyUsage', value: 0, icon: ChartLine },
    { key: 'resources', value: 0, icon: HardDrive },
    { key: 'joinedDays', value: joinedDays(colleague.joinedAt, today), icon: CalendarDays },
    { key: 'channels', value: colleague.imConfigured ? 1 : 0, icon: Monitor },
    { key: 'groupChats', value: 0, icon: UsersRound },
  ];

  return (
    <>
      <p className="jiuwenmate-profile__description">{colleague.description}</p>
      <p className="jiuwenmate-profile__joined">
        {t('jiuwenmate.profile.joinedAt', { time: formatJoinedAt(colleague.joinedAt) })}
      </p>
      {!colleague.imConfigured ? (
        <section className="jiuwenmate-profile__im" data-testid="jiuwenmate-profile-quick-im">
          <header>
            <strong>{t('jiuwenmate.profile.quickIm')}</strong>
            <button type="button" className="jiuwenmate-link-button" onClick={onConfigureIm}>
              <SquareArrowOutUpRight aria-hidden />
              {t('jiuwenmate.profile.goConfigure')}
            </button>
          </header>
          <div className="jiuwenmate-profile__im-row">
            <img src={feishuIcon} alt="" aria-hidden />
            <span>
              <strong>{t('jiuwenmate.im.feishu')}</strong>
              <small>{t('jiuwenmate.profile.feishuHint')}</small>
            </span>
            <button type="button" className="jiuwenmate-link-button jiuwenmate-link-button--blue" onClick={onConfigureIm}>
              {t('jiuwenmate.profile.authorize')}
            </button>
          </div>
        </section>
      ) : null}

      <section className="jiuwenmate-profile__block">
        <header>
          <h2>{t('jiuwenmate.profile.workLog')}</h2>
          <ActionMenu
            label={t('jiuwenmate.profile.workLog')}
            className="jiuwenmate-select-menu"
            trigger={
              <>
                <span>{t(`jiuwenmate.profile.periods.${period}`)}</span>
                <ChevronDown aria-hidden />
              </>
            }
            items={WORK_LOG_PERIODS.map((item) => ({
              key: item,
              label: t(`jiuwenmate.profile.periods.${item}`),
              onSelect: () => setPeriod(item),
            }))}
          />
        </header>
        <div className="jiuwenmate-metric-strip" data-testid="jiuwenmate-profile-metrics">
          {metrics.map(({ key, value, icon: Icon }) => (
            <div key={key}>
              <strong>{value}</strong>
              <span>
                <Icon aria-hidden />
                {t(`jiuwenmate.profile.metrics.${key}`)}
              </span>
            </div>
          ))}
        </div>
      </section>

      <section className="jiuwenmate-profile__block jiuwenmate-profile__block--card">
        <header>
          <h2>{t('jiuwenmate.profile.periodRecord')}</h2>
          <ActionMenu
            label={t('jiuwenmate.profile.periodRecord')}
            className="jiuwenmate-select-menu"
            trigger={
              <>
                <span>{t(half === 2 ? 'jiuwenmate.profile.secondHalf' : 'jiuwenmate.profile.firstHalf', { year })}</span>
                <ChevronDown aria-hidden />
              </>
            }
            items={[
              { key: 'second', label: t('jiuwenmate.profile.secondHalf', { year }), onSelect: () => setHalf(2) },
              { key: 'first', label: t('jiuwenmate.profile.firstHalf', { year }), onSelect: () => setHalf(1) },
            ]}
          />
        </header>
        <div className="jiuwenmate-profile__heat">
          <div>
            <ActivityHeatmap
              heatmap={heatmap}
              hoveredCellId={hoveredCellId}
              onHover={setHoveredCellId}
            />
            <HeatmapLegend />
          </div>
          <div className="jiuwenmate-task-ring">
            <span className="jiuwenmate-task-ring__circle">
              <strong>0</strong>
              <small>{t('jiuwenmate.profile.taskCount')}</small>
            </span>
            <dl>
              <dt>{t('jiuwenmate.profile.taskTypes')}</dt>
              {(['conversation', 'channel', 'schedule'] as const).map((kind) => (
                <dd key={kind} data-kind={kind}>
                  <i />
                  <span>{t(`jiuwenmate.profile.taskKinds.${kind}`)}</span>
                  <strong>0</strong>
                </dd>
              ))}
            </dl>
          </div>
        </div>
      </section>

      <ExecutionHistory />
    </>
  );
}

function ExecutionHistory() {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const columns = ['task', 'channel', 'status', 'tokens', 'lastResponse', 'actions'] as const;
  return (
    <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-history">
      <header>
        <h2>{t('jiuwenmate.profile.history')}</h2>
        <span className="jiuwenmate-profile__filters">
          <button type="button" className="jiuwenmate-select-button">
            {t('jiuwenmate.profile.allChannels')}
            <ChevronDown aria-hidden />
          </button>
          <button type="button" className="jiuwenmate-select-button">
            {t('jiuwenmate.profile.allStatus')}
            <ChevronDown aria-hidden />
          </button>
          <label className="jiuwenmate-search">
            <Search aria-hidden />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t('jiuwenmate.common.searchPlaceholder')}
              aria-label={t('jiuwenmate.common.searchPlaceholder')}
            />
          </label>
        </span>
      </header>
      <table className="jiuwenmate-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{t(`jiuwenmate.profile.historyColumns.${column}`)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          <tr>
            <td colSpan={columns.length} className="jiuwenmate-table__empty">
              {t('jiuwenmate.profile.historyEmpty')}
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  );
}

function sameNames(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((item, index) => item === right[index]);
}

function IdentityEditor({
  name,
  description,
  tags,
  saving,
  onNameChange,
  onDescriptionChange,
  onTagsChange,
  onSave,
}: {
  name: string;
  description: string;
  tags: readonly string[];
  saving: boolean;
  onNameChange: (value: string) => void;
  onDescriptionChange: (value: string) => void;
  onTagsChange: (value: string[]) => void;
  onSave: () => void;
}) {
  const { t } = useTranslation();
  const domains = [...new Set([...JIUWENMATE_WORK_DOMAINS, ...tags])];
  return (
    <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-identity-editor">
      <label className="jiuwenmate-field">
        <span>{t('jiuwenmate.create.name')}</span>
        <input value={name} onChange={(event) => onNameChange(event.target.value)} data-testid="jiuwenmate-profile-name-input" />
      </label>
      <label className="jiuwenmate-field">
        <span>{t('jiuwenmate.create.description')}</span>
        <textarea value={description} rows={3} onChange={(event) => onDescriptionChange(event.target.value)} />
      </label>
      <div className="jiuwenmate-field">
        <span>{t('jiuwenmate.create.role')}</span>
        <div className="jiuwenmate-capability-grid">
          {domains.map((domain) => (
            <button
              key={domain}
              type="button"
              className="jiuwenmate-capability-chip"
              aria-pressed={tags.includes(domain)}
              onClick={() => onTagsChange(tags.includes(domain) ? tags.filter((item) => item !== domain) : [...tags, domain])}
            >
              {domain}
            </button>
          ))}
        </div>
      </div>
      <div className="jiuwenmate-profile__resource-actions">
        <button type="button" className="jiuwenmate-dark-button" disabled={saving || name.trim().length === 0} onClick={onSave}>
          {saving ? t('jiuwenmate.profile.savingResources') : t('jiuwenmate.profile.saveResources')}
        </button>
      </div>
    </section>
  );
}

function ProfileSectionDetail({
  colleague,
  modelCatalog,
  section,
  editing,
  name,
  description,
  tags,
  model,
  skills,
  connectors,
  plugins,
  resourceCatalog,
  savingResources,
  onNameChange,
  onDescriptionChange,
  onTagsChange,
  onModelChange,
  onSkillsChange,
  onConnectorsChange,
  onPluginsChange,
  onSaveSettings,
  onConfigureIm,
}: {
  colleague: JiuwenMateColleague;
  modelCatalog: JiuwenMateModelCatalog;
  section: Exclude<JiuwenMateProfileSection, 'overview'>;
  editing: boolean;
  name: string;
  description: string;
  tags: readonly string[];
  model: string;
  skills: readonly string[];
  connectors: readonly string[];
  plugins: readonly string[];
  resourceCatalog?: ColleagueResourceCatalog;
  savingResources: boolean;
  onNameChange: (value: string) => void;
  onDescriptionChange: (value: string) => void;
  onTagsChange: (value: string[]) => void;
  onModelChange: (value: string) => void;
  onSkillsChange: (items: string[]) => void;
  onConnectorsChange: (items: string[]) => void;
  onPluginsChange: (items: string[]) => void;
  onSaveSettings?: (edit: ColleagueSettingsEdit) => void | Promise<void>;
  onConfigureIm: () => void;
}) {
  const { t } = useTranslation();
  const title = t(`jiuwenmate.profile.sections.${section}`);

  if (section === 'persona') {
    return editing ? (
      <IdentityEditor
        name={name}
        description={description}
        tags={tags}
        saving={savingResources}
        onNameChange={onNameChange}
        onDescriptionChange={onDescriptionChange}
        onTagsChange={onTagsChange}
        onSave={() => onSaveSettings?.({ name: name.trim(), description: description.trim(), tags: [...tags] })}
      />
    ) : (
      <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
        <h2>{title}</h2>
        <dl className="jiuwenmate-profile__facts">
          <dt>{t('jiuwenmate.create.name')}</dt>
          <dd>{colleague.name}</dd>
          <dt>{t('jiuwenmate.create.description')}</dt>
          <dd>{colleague.description}</dd>
          <dt>{t('jiuwenmate.create.role')}</dt>
          <dd>{colleague.tags.length > 0 ? colleague.tags.join('、') : '-'}</dd>
        </dl>
      </section>
    );
  }

  if (section === 'model') {
    const choices = modelCatalog.models.some((item) => item.id === model) || model.length === 0
      ? modelCatalog.models
      : [{ id: model, label: model, provider: '' }, ...modelCatalog.models];
    const dirty = model !== colleague.capabilities.model;
    return (
      <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
        <h2>{title}</h2>
        {editing ? (
          <>
            <label className="jiuwenmate-field">
              <span>{t('jiuwenmate.create.sections.model')}</span>
              <select value={model} onChange={(event) => onModelChange(event.target.value)} data-testid="jiuwenmate-profile-model-select">
                {choices.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
            <div className="jiuwenmate-profile__resource-actions">
              <button
                type="button"
                className="jiuwenmate-dark-button"
                disabled={!dirty || savingResources || model.length === 0}
                onClick={() => onSaveSettings?.({ model })}
              >
                {savingResources ? t('jiuwenmate.profile.savingResources') : t('jiuwenmate.profile.saveResources')}
              </button>
            </div>
          </>
        ) : (
          <div className="jiuwenmate-model-card">
            <ModelLabel catalog={modelCatalog} modelId={colleague.capabilities.model} />
          </div>
        )}
      </section>
    );
  }

  if (section === 'channels') {
    return (
      <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
        <h2>{title}</h2>
        <div className="jiuwenmate-profile__im-row jiuwenmate-profile__im-row--bordered">
          <img src={feishuIcon} alt="" aria-hidden />
          <span>
            <strong>{t('jiuwenmate.im.feishu')}</strong>
            <small>{t('jiuwenmate.profile.feishuHint')}</small>
          </span>
          {colleague.imConfigured ? (
            <em className="jiuwenmate-badge" data-tone="success">{t('jiuwenmate.profile.authorized')}</em>
          ) : (
            <button type="button" className="jiuwenmate-link-button jiuwenmate-link-button--blue" onClick={onConfigureIm}>
              {t('jiuwenmate.profile.authorize')}
            </button>
          )}
        </div>
      </section>
    );
  }

  if (section === 'schedules') {
    return (
      <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
        <h2>{title}</h2>
        <div className="jiuwenmate-empty-state">
          <img src={scheduleEmpty} alt="" aria-hidden />
          <p>{t('jiuwenmate.workspace.noSchedules')}</p>
        </div>
      </section>
    );
  }

  if (RESOURCE_SECTIONS.some((item) => item === section) && onSaveSettings) {
    const resourceSection = section as ResourceSection;
    const selected = resourceSection === 'skills' ? skills : resourceSection === 'connectors' ? connectors : plugins;
    const catalog = resourceCatalog?.[resourceSection] ?? null;
    const changeSelected = resourceSection === 'skills' ? onSkillsChange : resourceSection === 'connectors' ? onConnectorsChange : onPluginsChange;
    const dirty =
      !sameNames(skills, colleague.capabilities.skills) ||
      !sameNames(connectors, colleague.capabilities.connectors) ||
      !sameNames(plugins, colleague.capabilities.plugins);
    const emptyKey =
      resourceSection === 'skills'
        ? 'jiuwenmate.profile.catalogEmptySkills'
        : resourceSection === 'connectors'
          ? 'jiuwenmate.profile.catalogEmptyConnectors'
          : 'jiuwenmate.profile.catalogEmptyPlugins';
    return (
      <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
        <header>
          <h2>{title}</h2>
          <ActionMenu
            label={t(`jiuwenmate.create.add.${resourceSection}`)}
            className="jiuwenmate-collapsible__add"
            testId="jiuwenmate-profile-resource-add"
            trigger={
              <>
                <Plus aria-hidden />
                <span>{t(`jiuwenmate.create.add.${resourceSection}`)}</span>
              </>
            }
            items={(catalog ?? [])
              .filter((option) => !selected.includes(option.name))
              .map((option) => ({
                key: option.name,
                label: option.label,
                onSelect: () => changeSelected([...selected, option.name]),
              }))}
          />
        </header>
        <p className="jiuwenmate-capability-empty">{t('jiuwenmate.profile.resourcesHint')}</p>
        {resourceCatalog && !resourceCatalog.ready ? (
          <p className="jiuwenmate-capability-empty">{t('jiuwenmate.profile.catalogLoading')}</p>
        ) : null}
        {resourceCatalog?.ready && catalog === null ? (
          <p className="jiuwenmate-capability-empty">{t('jiuwenmate.profile.catalogFailed')}</p>
        ) : null}
        {resourceCatalog?.ready && catalog !== null && catalog.length === 0 && selected.length === 0 ? (
          <p className="jiuwenmate-capability-empty">{t(emptyKey)}</p>
        ) : null}
        {selected.length === 0 && catalog && catalog.length > 0 ? (
          <p className="jiuwenmate-capability-empty">{t(`jiuwenmate.create.empty.${resourceSection}`)}</p>
        ) : null}
        {selected.length > 0 ? (
          <div className="jiuwenmate-capability-grid">
            {selected.map((item) => (
              <span key={item} className="jiuwenmate-capability-chip" data-kind={resourceSection}>
                <span>{catalog?.find((option) => option.name === item)?.label ?? item}</span>
                <button
                  type="button"
                  aria-label={`${t('jiuwenmate.create.remove')} ${item}`}
                  onClick={() => changeSelected(selected.filter((value) => value !== item))}
                >
                  <X aria-hidden />
                </button>
              </span>
            ))}
          </div>
        ) : null}
        <div className="jiuwenmate-profile__resource-actions">
          <button
            type="button"
            className="jiuwenmate-dark-button"
            disabled={!dirty || savingResources}
            onClick={() => onSaveSettings({ skills: [...skills], connectors: [...connectors], plugins: [...plugins] })}
            data-testid="jiuwenmate-profile-resource-save"
          >
            {savingResources ? t('jiuwenmate.profile.savingResources') : t('jiuwenmate.profile.saveResources')}
          </button>
        </div>
      </section>
    );
  }

  const items = colleague.capabilities[section];
  return (
    <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
      <h2>{title}</h2>
      {section === 'knowledge' && colleague.workspaceDir ? (
        <div data-testid="jiuwenmate-profile-workspace-dir">
          <dl className="jiuwenmate-profile__facts">
            <dt>{t('jiuwenmate.profile.workspaceDir')}</dt>
            <dd>
              <code>{colleague.workspaceDir}</code>
            </dd>
          </dl>
          <p className="jiuwenmate-capability-empty">{t('jiuwenmate.profile.workspaceDirHint')}</p>
        </div>
      ) : null}
      {items.length === 0 ? (
        <p className="jiuwenmate-capability-empty">{t(`jiuwenmate.create.empty.${section}`)}</p>
      ) : (
        <div className="jiuwenmate-capability-grid">
          {items.map((item) => (
            <span key={item} className="jiuwenmate-capability-chip">
              {item}
            </span>
          ))}
        </div>
      )}
    </section>
  );
}
