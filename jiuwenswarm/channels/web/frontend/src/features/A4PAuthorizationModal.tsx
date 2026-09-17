import { useEffect, useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useChatStore, useSessionStore } from '../stores';
import type { A4PAuthorizationRequest } from '../stores/chatStore';
import { webClient } from '../services/webClient';
import { getPasskeyAssertion } from './a4p/webauthn';

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
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
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
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const isConnected = useSessionStore((state) => state.isConnected);
  const pending = useChatStore(
    (state) => state.runtimes[activeSessionId ?? '']?.pendingA4PAuthorization ?? null,
  );
  const setPendingAuthorization = useChatStore(
    (state) => state.setPendingA4PAuthorization,
  );
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
    const requestAtStart = useChatStore.getState()
      .runtimes[activeSessionId]?.pendingA4PAuthorization?.requestId;
    void webClient.request<{ pending?: A4PAuthorizationRequest | null }>(
      'a4p.authorization.pending',
      { session_id: activeSessionId },
    ).then((result) => {
      if (cancelled) {
        return;
      }
      const currentRequest = useChatStore.getState()
        .runtimes[activeSessionId]?.pendingA4PAuthorization?.requestId;
      if (result.pending || currentRequest === requestAtStart) {
        setPendingAuthorization(activeSessionId, result.pending ?? null);
      }
    }).catch(() => {
      // A4P can be disabled; the push path remains authoritative in that case.
    });
    return () => {
      cancelled = true;
    };
  }, [activeSessionId, isConnected, setPendingAuthorization]);

  if (!pending) {
    return null;
  }

  const setPending = (request: typeof pending | null) => {
    if (activeSessionId) {
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
    setBusy(true);
    setError(null);
    try {
      const signingOptions = asRecord(pending.signingOptions);
      const signatureMethod = String(signingOptions.signatureMethod ?? '');
      let assertion: Record<string, unknown> | undefined;
      if (signatureMethod === 'webauthn') {
        const publicKey = asRecord(signingOptions.methodOptions);
        assertion = await getPasskeyAssertion(
          publicKey as unknown as PublicKeyCredentialRequestOptionsJSON,
        );
      }
      await webClient.request(
        'a4p.authorization.complete',
        {
          requestId: pending.requestId,
          ...(assertion ? { assertion } : {}),
          session_id: activeSessionId,
        },
        { timeoutMs: 300000 }
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
        { timeoutMs: 300000 }
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
  const signatureMethod = String(
    asRecord(pending.signingOptions).signatureMethod ?? '',
  );
  const requiresPasskey = signatureMethod === 'webauthn';
  const actionLines = actions.map((rawAction) => {
    const action = asRecord(rawAction);
    const name = typeof action.name === 'string' && action.name ? action.name : 'unknown';
    const params = asRecord(action.params);
    const config = ACTION_DISPLAY_CONFIG[name];
    if (!config || !(config.primaryParam in params)) {
      return `- ${name}\n${JSON.stringify(params, null, 2)}`;
    }
    const lines = [
      `- ${t(config.labelKey)} (${name}): ${formatDisplayValue(params[config.primaryParam])}`,
    ];
    Object.entries(params).forEach(([key, value]) => {
      if (key !== config.primaryParam) {
        lines.push(`  ${key}: ${formatDisplayValue(value)}`);
      }
    });
    return lines.join('\n');
  });
  const validTime = asRecord(pending.mandate.validTime);
  const reason =
    typeof uiContext.reason === 'string' && uiContext.reason.trim()
      ? uiContext.reason.trim()
      : t('a4pAuthorization.defaultReason');
  const displaySections: Array<{ title: string; content: string }> = [];

  if (cronJobId) {
    const schedule = asRecord(authorizationTarget.schedule);
    const cronLines = [`${t('a4pAuthorization.taskId')}: ${cronJobId}`];
    if (typeof authorizationTarget.description === 'string' && authorizationTarget.description) {
      cronLines.push(
        `${t('a4pAuthorization.taskInstructions')}: ${authorizationTarget.description}`,
      );
    }
    if (typeof schedule.cronExpression === 'string' && schedule.cronExpression) {
      const timezone =
        typeof schedule.timezone === 'string' && schedule.timezone
          ? ` (${schedule.timezone})`
          : '';
      cronLines.push(
        `${t('a4pAuthorization.schedule')}: ${schedule.cronExpression}${timezone}`,
      );
    }
    displaySections.push({
      title: t('a4pAuthorization.scheduledTask'),
      content: cronLines.join('\n'),
    });
  }

  displaySections.push({
    title: t('a4pAuthorization.authorizedActions'),
    content: actionLines.length > 0 ? actionLines.join('\n') : t('a4pAuthorization.noActions'),
  });
  displaySections.push({
    title: t('a4pAuthorization.validTime'),
    content: `${t('a4pAuthorization.beijingTime')}: ${formatBeijingTime(validTime.start)} ${t('a4pAuthorization.timeRangeTo')} ${formatBeijingTime(validTime.end)}`,
  });
  displaySections.push({
    title: t('a4pAuthorization.reason'),
    content: reason,
  });

  return (
    <div className="animate-rise mx-2 my-3">
      <div
        className="w-full overflow-hidden rounded-lg"
        style={{
          border: '1px solid var(--color-action-primary)',
          backgroundColor: 'var(--color-surface-card)',
        }}
      >
        <div
          className="flex items-center justify-between px-4 py-2.5"
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

        <div className="space-y-3 px-4 py-3">
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
              className="block rounded-md px-2.5 py-2 text-xs"
              style={{
                border: '1px solid var(--color-border-default)',
                backgroundColor: 'var(--color-surface-elevated)',
                color: 'var(--color-text-primary)',
              }}
            >
              {pending.requestId}
            </code>
          </div>

          <div className="space-y-2">
            {displaySections.map((section, index) => (
              <div className="space-y-1.5" key={`${section.title}-${index}`}>
                <div
                  className="text-[11px] font-semibold"
                  style={{ color: 'var(--color-text-secondary)' }}
                >
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
            <summary className="cursor-pointer px-3 py-2 text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
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
              <>{t('a4pAuthorization.cronExplanation')}</>
            ) : (
              <>{t('a4pAuthorization.sessionExplanation')}</>
            )}
          </div>

          {error && (
            <div className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
              {error}
            </div>
          )}
        </div>

        <div
          className="flex items-center justify-end gap-2 px-4 py-3"
          style={{
            borderTop: '1px solid var(--color-border-default)',
            backgroundColor: 'var(--color-surface-panel-strong)',
          }}
        >
          <button type="button" className="btn" onClick={reject} disabled={busy}>
            {t('a4pAuthorization.reject')}
          </button>
          <button type="button" className="btn primary" onClick={approve} disabled={busy}>
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
