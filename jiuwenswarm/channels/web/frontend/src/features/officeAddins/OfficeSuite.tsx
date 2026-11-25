import { useCallback, useEffect, useId, useState, type ReactNode } from 'react';
import { AlertCircle, CheckCircle2, Info } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import officeSuiteIcon from '../../assets/office-addins/suite-office.svg';
import wpsSuiteIcon from '../../assets/office-addins/suite-wps.png';
import BackIcon from '../../assets/work-mode/arrow-left.svg?react';
import { Button } from '../../components/ui/Button/Button';
import { ConfirmDialog } from '../../components/ConnectorMarket/ConfirmDialog';
import { MarketCard } from '../../components/ConnectorMarket/MarketCard';
import type { McpCardState } from '../../components/ConnectorMarket/mcpState';
import { DetailSection } from '../../components/ui/DetailSection/DetailSection';
import { Dialog } from '../../components/ui/Dialog/Dialog';
import { EntityHeader, EntityTagList } from '../../components/ui/EntityHeader/EntityHeader';
import { APP_LOGOS } from './appLogos';
import {
  SUITE_CATALOG,
  appStatus,
  fetchOfficeAddins,
  installOfficeSuite,
  removeOfficeSuite,
  suiteState,
  type AppId,
  type AppStatus,
  type SuiteId,
  type SuiteState,
  type SuiteStatus,
  type Suites,
} from './officeAddins';
import './OfficeSuite.css';

/** Every suite's status, read again when the window regains focus; null where the desktop app does not serve it. */
export function useOfficeSuites() {
  const [suites, setSuites] = useState<Suites | null>(null);
  const refresh = useCallback(() => {
    fetchOfficeAddins()
      .then(setSuites)
      .catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
    // An Office app can be installed, opened or quit while the user is away from this window.
    window.addEventListener('focus', refresh);
    return () => window.removeEventListener('focus', refresh);
  }, [refresh]);

  const setStatus = useCallback(
    (suite: SuiteId, status: SuiteStatus) => setSuites((latest) => ({ ...latest, [suite]: status })),
    [],
  );
  return { suites, setStatus };
}

// Each suite's card shows its own kinds of documents in its suite's colours.
const SUITE_ICONS: Record<SuiteId, string> = { office: officeSuiteIcon, wps: wpsSuiteIcon };

// The plaza card's states: installed reads as ready to use; a problem, or no Office app found, as the card error state.
const CARD_STATE: Record<SuiteState['kind'], McpCardState> = {
  notInstalled: 'idle',
  installed: 'connected',
  needsRepair: 'error',
  notFound: 'error',
  outdated: 'error',
};

/** A suite in the plugin plaza. Every action opens the detail page, where installing first explains the certificate prompt. */
export function OfficeSuiteCard({ suite, status, onOpen }: { suite: SuiteId; status: SuiteStatus; onOpen: () => void }) {
  const { t } = useTranslation();
  const { kind } = suiteState(status);
  return (
    <MarketCard
      title={t(`officeAddins.suites.${suite}.title`)}
      description={t(`officeAddins.suites.${suite}.what`)}
      iconUrl={SUITE_ICONS[suite]}
      state={CARD_STATE[kind]}
      errorLabel={t(`officeAddins.appStatus.${kind}`)}
      canOpenDetail
      onOpenDetail={onOpen}
      onQuickAdd={onOpen}
      onUse={onOpen}
    />
  );
}

// No app of the suite on this computer leaves nothing to install until one has run once.
const PRIMARY_ACTION: Record<SuiteState['kind'], 'install' | 'repair' | 'remove' | null> = {
  notFound: null,
  outdated: null,
  notInstalled: 'install',
  needsRepair: 'repair',
  installed: 'remove',
};

// Status as the settings page's external CLI rows show it (ExternalCliAgentsSection): small text with a check or alert icon.
// A suite's state uses the same table, since it has the same kinds as an app's status.
const STATUS_LOOK: Record<AppStatus, { tone: string; icon?: ReactNode }> = {
  installed: { tone: 'text-ok', icon: <CheckCircle2 size={14} /> },
  notInstalled: { tone: 'text-text-muted' },
  needsRepair: { tone: 'text-warn', icon: <AlertCircle size={14} /> },
  notFound: { tone: 'text-text-muted' },
  outdated: { tone: 'text-warn', icon: <AlertCircle size={14} /> },
};

function StatusLabel({ status, testId }: { status: AppStatus; testId: string }) {
  const { t } = useTranslation();
  const { tone, icon } = STATUS_LOOK[status];
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1 text-xs ${tone}`}
      data-testid={testId}
      data-variant={status}
    >
      {icon}
      {t(`officeAddins.appStatus.${status}`)}
    </span>
  );
}

function AppLogo({ app, className }: { app: AppId; className: string }) {
  const Logo = APP_LOGOS[app];
  if (typeof Logo === 'string') return <img src={Logo} alt="" className={className} data-testid="office-addins-logo" />;
  return <Logo className={`${className} text-text-muted`} aria-hidden="true" data-testid="office-addins-logo" />;
}

/** The plugin detail page's layout: back link, heading with the suite's apps as tags, problems, then sections. */
export function OfficeSuiteDetail({
  suite,
  status,
  onStatus,
  onBack,
}: {
  suite: SuiteId;
  status: SuiteStatus;
  onStatus: (status: SuiteStatus) => void;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const confirmTitleId = useId();
  const state = suiteState(status);
  const action = PRIMARY_ACTION[state.kind];
  const { apps, sections, steps } = SUITE_CATALOG[suite];
  const title = t(`officeAddins.suites.${suite}.title`);
  const [pending, setPending] = useState<'install' | 'remove' | null>(null);
  const [failed, setFailed] = useState(false);
  const [confirming, setConfirming] = useState<'install' | 'repair' | 'remove' | null>(null);
  const missing = apps.filter((app) => appStatus(status, app) === 'notFound').map((app) => t(`officeAddins.apps.${app}`));
  const outdated = apps.filter((app) => appStatus(status, app) === 'outdated');

  const run = async (request: 'install' | 'remove') => {
    setPending(request);
    setFailed(false);
    try {
      const next = await (request === 'install' ? installOfficeSuite : removeOfficeSuite)(suite);
      onStatus(next);
      // Still installed after remove when the system kept trusting the certificate, as after a declined password prompt.
      setFailed(request === 'remove' && next.installed);
    } catch {
      setFailed(true);
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="office-addins-detail" data-variant={suite}>
      <button type="button" onClick={onBack} className="detail-back" data-testid="office-addins-back">
        <BackIcon aria-hidden="true" />
        {t('connectorMarket.common.back')}
      </button>

      <div className="detail-body relative min-h-0 flex-1 overflow-y-auto pb-6">
        <div className="office-addins-heading flex flex-col gap-3">
          <EntityHeader
            variant="detail"
            avatar={null}
            title={title}
            titleTestId="office-addins-title"
            titleEnd={
              <span className="entity-header__tag">
                <StatusLabel status={state.kind} testId="office-addins-suite-status" />
              </span>
            }
            actions={
              <div className="detail-actions" data-testid="office-addins-detail-actions">
                {action === 'remove' && (
                  <button
                    type="button"
                    className="detail-action detail-action--use"
                    disabled={pending !== null}
                    onClick={() => setConfirming('remove')}
                    data-testid="office-addins-remove"
                  >
                    {t(pending === 'remove' ? 'officeAddins.actions.removing' : 'officeAddins.actions.remove')}
                  </button>
                )}
                {(action === 'install' || action === 'repair') && (
                  <button
                    type="button"
                    className="detail-action detail-action--install"
                    disabled={pending !== null}
                    aria-busy={pending === 'install' || undefined}
                    onClick={() => setConfirming(action)}
                    data-testid="office-addins-action"
                    data-variant={action}
                  >
                    {t(`officeAddins.actions.${pending === 'install' ? `${action}Pending` : action}`)}
                  </button>
                )}
              </div>
            }
          />
          <EntityTagList
            tags={apps.map((app) => ({
              label: t(`officeAddins.apps.${app}`),
              icon: <AppLogo app={app} className="h-4 w-4" />,
            }))}
          />
        </div>

        {failed && (
          <p className="text-xs text-danger" role="alert" data-testid="office-addins-notice">
            {t('officeAddins.errors.actionFailed')}
          </p>
        )}

        {/* Under the heading, as the plugin detail page shows what keeps an installed plugin from working. */}
        {state.kind === 'needsRepair' && (
          <div
            className="flex items-start gap-1.5 rounded-lg bg-[color:var(--color-feedback-danger-banner)] px-3 py-2 text-[13px] text-text-muted"
            data-testid="office-addins-problems"
          >
            <AlertCircle size={14} className="mt-[3px] shrink-0 text-danger" aria-hidden="true" />
            <ul className="flex flex-col gap-0.5">
              {state.problems.map((problem) => (
                <li key={problem} data-variant={problem}>
                  {t(`officeAddins.problems.${problem}`)}
                </li>
              ))}
            </ul>
          </div>
        )}

        <DetailSection title={t('officeAddins.sections.about')}>
          <p>{t(`officeAddins.suites.${suite}.what`)}</p>
        </DetailSection>

        <DetailSection title={t('officeAddins.sections.apps')} testId="office-addins-apps">
          <ul className="office-addins-detail-width divide-y divide-border rounded-xl border border-border bg-card">
            {apps.map((app) => {
              const appState = appStatus(status, app);
              return (
                <li
                  key={app}
                  className="flex items-center gap-3 px-4 py-2.5"
                  data-testid="office-addins-app"
                  data-variant={app}
                  data-status={appState}
                >
                  <AppLogo app={app} className="h-5 w-5 shrink-0" />
                  <span className="min-w-0 flex-1 text-text">{t(`officeAddins.apps.${app}`)}</span>
                  <StatusLabel status={appState} testId="office-addins-app-status" />
                </li>
              );
            })}
          </ul>
          {/* An app not on this computer is not a fault of the add-in: a quiet note under the list, not the banner. */}
          {missing.length > 0 && (
            <p
              className="office-addins-detail-width mt-2 flex items-start gap-1.5 text-[12px] leading-5 text-text-muted"
              data-testid="office-addins-apps-note"
            >
              <Info size={14} className="mt-[3px] shrink-0" aria-hidden="true" />
              {t('officeAddins.appsNotFound', { apps: missing.join(t('officeAddins.listSeparator')) })}
            </p>
          )}
          {outdated.map((app) => (
            <p
              key={app}
              className="office-addins-detail-width mt-2 flex items-start gap-1.5 text-[12px] leading-5 text-text-muted"
              data-testid="office-addins-app-outdated"
            >
              <Info size={14} className="mt-[3px] shrink-0" aria-hidden="true" />
              {t('officeAddins.appOutdated', { app: t(`officeAddins.apps.${app}`), version: status.required?.[app] })}
            </p>
          ))}
        </DetailSection>

        {/* Label and value rows in a small card, as the MCP detail page shows its connection config. */}
        <DetailSection title={t('officeAddins.sections.guide')} testId="office-addins-guide">
          <dl className="office-addins-detail-width grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 rounded-xl border border-border bg-card p-4 text-[12px] leading-5">
            {sections.map((section) => (
              <div key={section} className="contents" data-testid={`office-addins-${section}`}>
                <dt className="text-text-muted">{t(`officeAddins.sections.${section}`)}</dt>
                <dd className="m-0 text-text">
                  {section === 'start' ? (
                    <ol className="list-decimal pl-4">
                      {steps.map((step) => (
                        <li key={step}>{t(`officeAddins.suites.${suite}.steps.${step}`)}</li>
                      ))}
                    </ol>
                  ) : (
                    t(`officeAddins.suites.${suite}.${section}`)
                  )}
                </dd>
              </div>
            ))}
          </dl>
        </DetailSection>
      </div>

      <Dialog
        open={confirming === 'install' || confirming === 'repair'}
        titleId={confirmTitleId}
        className="max-w-[400px]"
        onCancel={() => setConfirming(null)}
        onBackdropClick={() => setConfirming(null)}
      >
        {(confirming === 'install' || confirming === 'repair') && (
          <div className="flex flex-col gap-3 p-5" data-testid="office-addins-confirm">
            <h2 id={confirmTitleId} className="text-[15px] font-semibold text-text-strong">
              {t(`officeAddins.confirm.${confirming}`, { name: title })}
            </h2>
            <p className="text-[13px] leading-5 text-text-muted">{t(`officeAddins.suites.${suite}.installNotice`)}</p>
            <div className="flex justify-end gap-2">
              <Button size="sm" onClick={() => setConfirming(null)} data-testid="office-addins-confirm-cancel">
                {t('common.cancel')}
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={() => {
                  setConfirming(null);
                  void run('install');
                }}
                data-testid="office-addins-confirm-ok"
              >
                {t(`officeAddins.actions.${confirming}`)}
              </Button>
            </div>
          </div>
        )}
      </Dialog>

      {confirming === 'remove' && (
        <ConfirmDialog
          title={t('officeAddins.confirm.remove', { name: title })}
          message={t(`officeAddins.suites.${suite}.removeNotice`)}
          confirmLabel={t('officeAddins.actions.remove')}
          busy={pending === 'remove'}
          onCancel={() => setConfirming(null)}
          onConfirm={async () => {
            await run('remove');
            setConfirming(null);
          }}
        />
      )}
    </div>
  );
}
