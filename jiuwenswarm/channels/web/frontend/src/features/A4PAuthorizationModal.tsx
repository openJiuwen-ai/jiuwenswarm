import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useChatStore, useSessionStore } from '../stores';
import type { A4PAuthorizationRequest } from '../stores/chatStore';
import { webClient } from '../services/webClient';
import { getPasskeyAssertion } from './a4p/webauthn';
import './a4p/authorizationCard.css';

const ACTION_DISPLAY_CONFIG: Record<string, { labelKey: string; primaryParam: string }> = {
  bash: { labelKey: 'a4pAuthorization.tools.bash', primaryParam: 'command' },
  mcp_exec_command: {
    labelKey: 'a4pAuthorization.tools.mcpExecCommand',
    primaryParam: 'command',
  },
  create_terminal: {
    labelKey: 'a4pAuthorization.tools.createTerminal',
    primaryParam: 'cmd',
  },
  write_file: { labelKey: 'a4pAuthorization.tools.writeFile', primaryParam: 'file_path' },
  edit_file: { labelKey: 'a4pAuthorization.tools.editFile', primaryParam: 'file_path' },
  acp_chat: { labelKey: 'a4pAuthorization.tools.acpChat', primaryParam: 'agent' },
};

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function formatDisplayValue(value: unknown): string {
  if (typeof value === 'string') {
    return value;
  }
  return JSON.stringify(value) ?? String(value);
}

function formatBeijingTime(value: unknown): string {
  if (typeof value !== 'string') {
    return '-';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day} ${values.hour}:${values.minute}:${values.second}`;
}

export function A4PAuthorizationCard() {
  const { t } = useTranslation();
  const scopeId = useId();
  const cardRef = useRef<HTMLDivElement>(null);
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const isConnected = useSessionStore((state) => state.isConnected);
  const pending = useChatStore((state) => state.runtimes[activeSessionId ?? '']?.pendingA4PAuthorization ?? null);
  const setPendingAuthorization = useChatStore((state) => state.setPendingA4PAuthorization);
  const [selection, setSelection] = useState<{ requestId: string; indexes: number[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
  }, [pending?.requestId]);

  useEffect(() => {
    if (!activeSessionId || !isConnected) {
      return;
    }
    let cancelled = false;
    const requestAtStart = useChatStore.getState().runtimes[activeSessionId]?.pendingA4PAuthorization?.requestId;
    void webClient
      .request<{ pending?: A4PAuthorizationRequest | null }>('a4p.authorization.pending', {
        session_id: activeSessionId,
      })
      .then((result) => {
        if (cancelled) {
          return;
        }
        const currentRequest = useChatStore.getState().runtimes[activeSessionId]?.pendingA4PAuthorization?.requestId;
        if (currentRequest === requestAtStart) {
          setPendingAuthorization(activeSessionId, result.pending ?? null);
        }
      })
      .catch(() => {
        // A4P can be disabled; the push path remains authoritative in that case.
      });
    return () => {
      cancelled = true;
    };
  }, [activeSessionId, isConnected, setPendingAuthorization]);

  useLayoutEffect(() => {
    const card = cardRef.current;
    if (!card) return;
    const shell = card.closest<HTMLElement>('.chat-panel-shell');
    const compose = card.closest<HTMLElement>('.chat-compose');
    let frame = 0;
    const measure = () => {
      const viewport = window.visualViewport;
      const viewportTop = viewport?.offsetTop ?? 0;
      const viewportHeight = viewport?.height ?? window.innerHeight;
      const viewportLeft = viewport?.offsetLeft ?? 0;
      const viewportWidth = viewport?.width ?? window.innerWidth;
      // The application has a desktop minimum width; keep A4P actions in view even below it.
      card.style.setProperty(
        '--a4p-card-max-width',
        `${Math.max(
          0,
          viewportLeft + viewportWidth - Math.max(viewportLeft, card.getBoundingClientRect().left) - 8,
        )}px`,
      );
      let available = viewportHeight * 0.65;
      if (shell && compose) {
        const bounds = shell.getBoundingClientRect();
        const visibleHeight = Math.max(
          0,
          Math.min(bounds.bottom, viewportTop + viewportHeight) - Math.max(bounds.top, viewportTop),
        );
        // Reserve the input, goal/activity rows, shell header and disclaimer.
        // The card's own height cancels out, so resizing its body cannot inflate the budget.
        const composeRemainder = Math.max(0, compose.scrollHeight - card.getBoundingClientRect().height);
        const surroundingHeight = Array.from(shell.children).reduce((height, child) => {
          if (!(child instanceof window.HTMLElement) || child === compose || child.classList.contains('chat-scroll'))
            return height;
          const style = window.getComputedStyle(child);
          if (style.position === 'absolute' || style.position === 'fixed') return height;
          return (
            height +
            child.getBoundingClientRect().height +
            (parseFloat(style.marginTop) || 0) +
            (parseFloat(style.marginBottom) || 0)
          );
        }, 0);
        available = Math.min(available, Math.max(0, visibleHeight - composeRemainder - surroundingHeight - 8));
        const headerHeight = card.querySelector<HTMLElement>('.a4p-authorization-header')?.offsetHeight ?? 0;
        const footerHeight = card.querySelector<HTMLElement>('.a4p-authorization-footer')?.offsetHeight ?? 0;
        if (available < headerHeight + footerHeight + 64) {
          // At high zoom there may not be room for both approval and the composer.
          // Prioritize the approval controls, bounded by the visible chat panel.
          available = Math.min(
            viewportHeight * 0.65,
            Math.max(0, Math.min(bounds.bottom, viewportTop + viewportHeight) - card.getBoundingClientRect().top - 8),
          );
        }
      }
      card.style.setProperty('--a4p-card-max-height', `${Math.floor(available)}px`);
    };
    const schedule = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(measure);
    };
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule);
    observer?.observe(card);
    if (shell) {
      observer?.observe(shell);
      Array.from(shell.children).forEach((child) => observer?.observe(child));
    }
    if (compose) observer?.observe(compose);
    window.addEventListener('resize', schedule);
    window.visualViewport?.addEventListener('resize', schedule);
    window.visualViewport?.addEventListener('scroll', schedule);
    return () => {
      window.cancelAnimationFrame(frame);
      observer?.disconnect();
      window.removeEventListener('resize', schedule);
      window.visualViewport?.removeEventListener('resize', schedule);
      window.visualViewport?.removeEventListener('scroll', schedule);
    };
  }, [pending?.requestId]);

  if (!pending) {
    return null;
  }

  const setPending = (request: typeof pending | null) => {
    if (
      activeSessionId &&
      useChatStore.getState().runtimes[activeSessionId]?.pendingA4PAuthorization?.requestId === pending.requestId
    ) {
      setPendingAuthorization(activeSessionId, request);
    }
  };
  const uiContext = pending.uiContext ?? {};
  const authorizationTarget = asRecord(uiContext.authorizationTarget);
  const cronJobId =
    typeof uiContext.cronJobId === 'string'
      ? uiContext.cronJobId
      : typeof authorizationTarget.cronJobId === 'string'
        ? authorizationTarget.cronJobId
        : '';
  const intent = asRecord(pending.mandate.intent);
  const actions = Array.isArray(intent.actions) ? intent.actions : [];
  const candidates = pending.originalActions?.length ? pending.originalActions : actions;
  const preparedIndexes = pending.preparedActionIndexes?.length
    ? pending.preparedActionIndexes
    : candidates.map((_, index) => index);
  const selectedIndexes = selection?.requestId === pending.requestId ? selection.indexes : preparedIndexes;
  const changed =
    selectedIndexes.length !== preparedIndexes.length ||
    selectedIndexes.some((index) => !preparedIndexes.includes(index));
  const working = busy || pending.repreparing === true;
  const modifyRange = async (indexes: number[]) => {
    if (working) return;
    setSelection({ requestId: pending.requestId, indexes });
    setError(null);
    const rangeChanged =
      indexes.length !== preparedIndexes.length || indexes.some((index) => !preparedIndexes.includes(index));
    if (!rangeChanged || !indexes.length) return;
    setBusy(true);
    try {
      const result = await webClient.request<{ pending: A4PAuthorizationRequest }>(
        'a4p.authorization.reprepare',
        { requestId: pending.requestId, session_id: activeSessionId, selectedActionIndexes: indexes },
        { timeoutMs: 300000 },
      );
      setPending(result.pending);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  const hasWildcardBashScope = actions.some((rawAction) => {
    if (!rawAction || typeof rawAction !== 'object' || Array.isArray(rawAction)) {
      return false;
    }
    const action = rawAction as Record<string, unknown>;
    if (action.name !== 'bash' || !action.params || typeof action.params !== 'object' || Array.isArray(action.params)) {
      return false;
    }
    const command = (action.params as Record<string, unknown>).command;
    return typeof command === 'string' && (command.includes('*') || command.includes('?'));
  });

  const approve = async () => {
    if (changed || working) return;
    setBusy(true);
    setError(null);
    try {
      const signingOptions = asRecord(pending.signingOptions);
      const signatureMethod = String(signingOptions.signatureMethod ?? '');
      let assertion: Record<string, unknown> | undefined;
      if (signatureMethod === 'webauthn') {
        const publicKey = asRecord(signingOptions.methodOptions);
        assertion = await getPasskeyAssertion(publicKey as unknown as PublicKeyCredentialRequestOptionsJSON);
      }
      await webClient.request(
        'a4p.authorization.complete',
        {
          requestId: pending.requestId,
          ...(assertion ? { assertion } : {}),
          session_id: activeSessionId,
        },
        { timeoutMs: 300000 },
      );
      setPending(null);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  const reject = async () => {
    setBusy(true);
    setError(null);
    try {
      await webClient.request(
        'a4p.authorization.reject',
        {
          requestId: pending.requestId,
          reason: 'User rejected A4P authorization',
          session_id: activeSessionId,
        },
        { timeoutMs: 300000 },
      );
      setPending(null);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  const authorizationObject = {
    requestId: pending.requestId,
    uiContext: pending.uiContext,
    mandate: pending.mandate,
    signingOptions: pending.signingOptions,
  };
  const authorizationJson = JSON.stringify(authorizationObject, null, 2);
  const signatureMethod = String(asRecord(pending.signingOptions).signatureMethod ?? '');
  const requiresPasskey = signatureMethod === 'webauthn';
  const validTime = asRecord(pending.mandate.validTime);
  const reason =
    typeof uiContext.reason === 'string' && uiContext.reason.trim()
      ? uiContext.reason.trim()
      : t('a4pAuthorization.defaultReason');
  const displaySections: Array<{ title: string; content: string }> = [];

  if (cronJobId) {
    const schedule = asRecord(authorizationTarget.schedule);
    const cronLines = [`${t('a4pAuthorization.taskId')}: ${cronJobId}`];
    if (typeof schedule.cronExpression === 'string' && schedule.cronExpression) {
      const timezone = typeof schedule.timezone === 'string' && schedule.timezone ? ` (${schedule.timezone})` : '';
      cronLines.push(`${t('a4pAuthorization.schedule')}: ${schedule.cronExpression}${timezone}`);
    }
    displaySections.push({
      title: t('a4pAuthorization.scheduledTask'),
      content: cronLines.join('\n'),
    });
  }

  displaySections.push({
    title: t('a4pAuthorization.validTime'),
    content: `${t('a4pAuthorization.beijingTime')}: ${formatBeijingTime(validTime.start)} ${t('a4pAuthorization.timeRangeTo')} ${formatBeijingTime(validTime.end)}`,
  });
  displaySections.push({
    title: t('a4pAuthorization.reason'),
    content: reason,
  });

  return (
    <div ref={cardRef} className="animate-rise mx-2 my-3" data-testid="a4p-authorization-card">
      <div
        className="a4p-authorization-panel w-full overflow-hidden rounded-lg"
        style={{
          border: '1px solid var(--color-action-primary)',
          backgroundColor: 'var(--color-surface-card)',
        }}
      >
        <div
          className="a4p-authorization-header flex items-center justify-between px-4 py-2.5"
          data-testid="a4p-authorization-header"
          style={{
            borderBottom: '1px solid var(--color-border-default)',
            backgroundColor: 'var(--color-surface-panel-strong)',
          }}
        >
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-3.5 w-3.5 flex-shrink-0" style={{ color: 'var(--color-action-primary)' }} />
            <span className="text-xs font-semibold" style={{ color: 'var(--color-action-primary)' }}>
              {t('a4pAuthorization.title')}
            </span>
          </div>
        </div>

        <div
          className="a4p-authorization-body space-y-3 px-4 py-3"
          data-testid="a4p-authorization-body"
          tabIndex={0}
          role="region"
          aria-label={t('a4pAuthorization.permissionScope')}
        >
          <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            {t('a4pAuthorization.requestLead')} {t('a4pAuthorization.intro')}
          </p>

          {hasWildcardBashScope && (
            <div className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
              {t('a4pAuthorization.wildcardWarning')}
            </div>
          )}

          <div className="space-y-1.5">
            <div className="text-[11px] font-semibold" style={{ color: 'var(--color-text-secondary)' }}>
              {t('a4pAuthorization.requestId')}
            </div>
            <code
              className="block break-all rounded-md px-2.5 py-2 text-xs"
              style={{
                border: '1px solid var(--color-border-default)',
                backgroundColor: 'var(--color-surface-elevated)',
                color: 'var(--color-text-primary)',
              }}
            >
              {pending.requestId}
            </code>
          </div>

          <fieldset
            data-testid="a4p-authorization-candidates"
            disabled={working}
            className="a4p-permission-options"
            aria-describedby={`${scopeId}-status`}
          >
            <legend className="a4p-permission-heading">
              <span data-testid="a4p-authorization-candidates-title">{t('a4pAuthorization.permissionScope')}</span>
              <span data-testid="a4p-authorization-selected-count" className="a4p-permission-count">
                {t('a4pAuthorization.selectedCount', { selected: selectedIndexes.length, total: candidates.length })}
              </span>
            </legend>
            <div className="a4p-permission-list" data-testid="a4p-authorization-options">
              {candidates.map((rawAction, index) => {
                const action = asRecord(rawAction);
                const name = typeof action.name === 'string' && action.name ? action.name : 'unknown';
                const config = ACTION_DISPLAY_CONFIG[name];
                const params = asRecord(action.params);
                const entries = Object.entries(params);
                const primary = config ? entries.filter(([key]) => key === config.primaryParam) : [];
                const remaining = entries.filter(([key]) => key !== config?.primaryParam);
                const selected = selectedIndexes.includes(index);
                const titleId = `${scopeId}-action-${index}`;
                const detailsId = `${titleId}-details`;
                return (
                  <label
                    key={index}
                    data-testid="a4p-authorization-action"
                    data-action-index={index}
                    data-variant={selected ? 'selected' : 'unselected'}
                    data-disabled={working}
                    className="a4p-permission-option"
                  >
                    <input
                      type="checkbox"
                      data-testid="a4p-authorization-action-checkbox"
                      aria-labelledby={titleId}
                      aria-describedby={detailsId}
                      checked={selected}
                      onChange={(event) =>
                        void modifyRange(
                          event.target.checked
                            ? [...selectedIndexes, index].sort((a, b) => a - b)
                            : selectedIndexes.filter((item) => item !== index),
                        )
                      }
                    />
                    <span className="a4p-permission-content">
                      <span id={titleId} className="a4p-permission-title" data-testid="a4p-authorization-action-title">
                        {config ? (
                          <>
                            {t(config.labelKey)} · <code>{name}</code>
                          </>
                        ) : (
                          <code>{name}</code>
                        )}
                      </span>
                      <span
                        id={detailsId}
                        className="a4p-permission-params"
                        data-testid="a4p-authorization-action-params"
                      >
                        {[...primary, ...remaining].map(([key, value]) => (
                          <span
                            key={key}
                            className="a4p-permission-param"
                            data-testid="a4p-authorization-action-param"
                            data-param={key}
                          >
                            <span className="a4p-permission-param-name">{key}</span>
                            <code className="a4p-permission-param-value">{formatDisplayValue(value)}</code>
                          </span>
                        ))}
                      </span>
                    </span>
                  </label>
                );
              })}
            </div>
            <p
              id={`${scopeId}-status`}
              role="status"
              data-testid="a4p-authorization-scope-status"
              className="a4p-permission-status"
            >
              {t(
                selectedIndexes.length === 0
                  ? 'a4pAuthorization.emptySelection'
                  : changed
                    ? error
                      ? 'a4pAuthorization.rangeUpdateFailed'
                      : 'a4pAuthorization.updatingRange'
                    : 'a4pAuthorization.readyRange',
              )}
            </p>
          </fieldset>

          <div className="space-y-2" data-testid="a4p-authorization-details">
            {displaySections.map((section, index) => (
              <div className="space-y-1.5" key={`${section.title}-${index}`}>
                <div className="text-[11px] font-semibold" style={{ color: 'var(--color-text-secondary)' }}>
                  {section.title}
                </div>
                <div
                  className="break-words rounded-md px-3 py-2 text-sm leading-6"
                  style={{
                    border: '1px solid var(--color-border-default)',
                    backgroundColor: 'var(--color-surface-elevated)',
                    color: 'var(--color-text-primary)',
                    whiteSpace: 'pre-line',
                  }}
                >
                  {section.content}
                </div>
              </div>
            ))}
          </div>

          <details
            className="rounded-md"
            style={{
              border: '1px solid var(--color-border-default)',
              backgroundColor: 'var(--color-surface-elevated)',
            }}
          >
            <summary
              className="cursor-pointer px-3 py-2 text-xs font-medium"
              style={{ color: 'var(--color-text-primary)' }}
            >
              {t('a4pAuthorization.fullAuthorizationObject')}
            </summary>
            <pre
              className="max-h-64 overflow-auto border-t px-3 py-2 text-[11px] leading-5"
              style={{
                borderColor: 'var(--color-border-default)',
                color: 'var(--color-text-secondary)',
              }}
            >
              {authorizationJson}
            </pre>
          </details>

          <div
            className="rounded-md px-3 py-2 text-xs"
            style={{
              border: '1px solid var(--color-border-default)',
              color: 'var(--color-text-secondary)',
            }}
          >
            {cronJobId ? (
              <>
                {t('a4pAuthorization.cronExplanation')} {t('a4pAuthorization.partialCronExplanation')}
              </>
            ) : (
              <>{t('a4pAuthorization.sessionExplanation')}</>
            )}
          </div>

          {error && (
            <div className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">{error}</div>
          )}
        </div>

        <div
          className="a4p-authorization-footer flex flex-wrap items-center justify-end gap-2 px-4 py-3"
          data-testid="a4p-authorization-footer"
          style={{
            borderTop: '1px solid var(--color-border-default)',
            backgroundColor: 'var(--color-surface-panel-strong)',
          }}
        >
          <button
            type="button"
            className="btn"
            data-testid="a4p-authorization-reject"
            onClick={reject}
            disabled={working}
          >
            {t('a4pAuthorization.reject')}
          </button>
          <button
            type="button"
            className="btn primary"
            data-testid="a4p-authorization-approve"
            onClick={approve}
            disabled={working || changed}
          >
            {busy
              ? t('a4pAuthorization.waiting')
              : requiresPasskey
                ? t('a4pAuthorization.approveWithPasskey')
                : t('a4pAuthorization.approve')}
          </button>
        </div>
      </div>
    </div>
  );
}
