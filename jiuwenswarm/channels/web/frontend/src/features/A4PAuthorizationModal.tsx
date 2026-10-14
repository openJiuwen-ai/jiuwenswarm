import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { FileText, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useChatStore, useSessionStore } from '../stores';
import type { A4PAuthorizationRequest } from '../stores/chatStore';
import { webClient } from '../services/webClient';
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

export function A4PAuthorizationCard() {
  const { t } = useTranslation();
  const scopeId = useId();
  const cardRef = useRef<HTMLDivElement>(null);
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const isConnected = useSessionStore((state) => state.isConnected);
  const pending = useChatStore((state) => state.runtimes[activeSessionId ?? '']?.pendingA4PAuthorization ?? null);
  const setPendingAuthorization = useChatStore((state) => state.setPendingA4PAuthorization);
  const [selection, setSelection] = useState<{ requestId: string; indexes: number[] } | null>(null);
  const [expansion, setExpansion] = useState<{ sessionId: string | null; requestId: string; indexes: number[] } | null>(
    null,
  );
  useEffect(() => {
    setExpansion(null);
  }, [activeSessionId, pending?.requestId]);
  const expandedIndexes =
    expansion?.sessionId === activeSessionId && expansion?.requestId === pending?.requestId
      ? (expansion?.indexes ?? [])
      : [];
  const [busy, setBusy] = useState(false);
  const submittingRef = useRef(false);
  const [errorState, setError] = useState<{ requestId: string; message: string } | null>(null);
  const error = errorState?.requestId === pending?.requestId ? errorState?.message : null;

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

  const setPending = (request: typeof pending | null, expectedRequestId = pending.requestId) => {
    if (
      activeSessionId &&
      useChatStore.getState().runtimes[activeSessionId]?.pendingA4PAuthorization?.requestId === expectedRequestId
    ) {
      setPendingAuthorization(activeSessionId, request);
    }
  };
  const uiContext = pending.uiContext ?? {};
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
  const modifyRange = (indexes: number[]) => {
    if (working || submittingRef.current) return;
    setSelection({ requestId: pending.requestId, indexes });
    setError(null);
  };
  const approve = async () => {
    if (!selectedIndexes.length || working || submittingRef.current || !activeSessionId) return;
    submittingRef.current = true;
    setBusy(true);
    setError(null);
    let requestId = pending.requestId;
    try {
      if (changed) {
        const result = await webClient.request<{ pending: A4PAuthorizationRequest }>(
          'a4p.authorization.reprepare',
          { requestId, session_id: activeSessionId, selectedActionIndexes: selectedIndexes },
          { timeoutMs: 300000 },
        );
        const state = useChatStore.getState();
        const currentRequestId = state.runtimes[activeSessionId]?.pendingA4PAuthorization?.requestId;
        // A replacement push may arrive before the RPC response. Only approve our own replacement.
        if (
          state.activeSessionId !== activeSessionId ||
          (currentRequestId !== requestId && currentRequestId !== result.pending.requestId)
        )
          return;
        setPending(result.pending);
        requestId = result.pending.requestId;
        if (useChatStore.getState().runtimes[activeSessionId]?.pendingA4PAuthorization?.requestId !== requestId) return;
      }
      await webClient.request(
        'a4p.authorization.complete',
        {
          requestId,
          session_id: activeSessionId,
        },
        { timeoutMs: 300000 },
      );
      setPending(null, requestId);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError({ requestId, message });
    } finally {
      submittingRef.current = false;
      setBusy(false);
    }
  };

  const reject = async () => {
    if (working || submittingRef.current) return;
    submittingRef.current = true;
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
      setError({ requestId: pending.requestId, message });
    } finally {
      submittingRef.current = false;
      setBusy(false);
    }
  };

  const reason =
    typeof uiContext.reason === 'string' && uiContext.reason.trim()
      ? uiContext.reason.trim()
      : t('a4pAuthorization.defaultReason');
  return (
    <div ref={cardRef} className="animate-rise mx-2 my-3" data-testid="a4p-authorization-card">
      <div className="a4p-authorization-panel" data-testid="a4p-authorization-panel">
        <div className="a4p-authorization-header" data-testid="a4p-authorization-header">
          <FileText size={16} aria-hidden="true" />
          <span data-testid="a4p-authorization-title">{t('a4pAuthorization.title')}</span>
        </div>

        <div
          className="a4p-authorization-body"
          data-testid="a4p-authorization-body"
          tabIndex={0}
          role="region"
          aria-label={t('a4pAuthorization.permissionScope')}
        >
          <div className="a4p-authorization-reason" data-testid="a4p-authorization-reason">
            <div className="a4p-authorization-reason-title" data-testid="a4p-authorization-reason-title">
              {t('a4pAuthorization.reason')}
            </div>
            <div className="a4p-authorization-reason-content" data-testid="a4p-authorization-reason-content">
              {reason}
            </div>
          </div>

          <fieldset
            data-testid="a4p-authorization-candidates"
            disabled={working}
            className="a4p-permission-options"
            aria-describedby={`${scopeId}-status`}
          >
            <legend className="a4p-permission-heading">
              <span data-testid="a4p-authorization-candidates-title">{t('a4pAuthorization.permissionScope')}</span>
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
                const expanded = expandedIndexes.includes(index);
                const titleId = `${scopeId}-action-${index}`;
                const detailsId = `${titleId}-details`;
                return (
                  <div
                    key={index}
                    data-testid="a4p-authorization-action"
                    data-action-index={index}
                    data-variant={index}
                    data-disabled={working}
                    className="a4p-permission-option"
                  >
                    <label className="a4p-permission-label" data-testid="a4p-authorization-action-label">
                      <input
                        type="checkbox"
                        data-testid="a4p-authorization-action-checkbox"
                        aria-labelledby={titleId}
                        aria-describedby={detailsId}
                        checked={selected}
                        onChange={(event) =>
                          modifyRange(
                            event.target.checked
                              ? [...selectedIndexes, index].sort((a, b) => a - b)
                              : selectedIndexes.filter((item) => item !== index),
                          )
                        }
                      />
                      <span id={titleId} className="a4p-permission-title" data-testid="a4p-authorization-action-title">
                        {config ? (
                          <>
                            {t(config.labelKey)} · <code>{name}</code>
                          </>
                        ) : (
                          <code>{name}</code>
                        )}
                      </span>
                    </label>
                    <button
                      type="button"
                      className="a4p-permission-toggle"
                      data-testid="a4p-authorization-action-toggle"
                      aria-expanded={expanded}
                      aria-controls={detailsId}
                      aria-label={t(expanded ? 'a4pAuthorization.collapseParams' : 'a4pAuthorization.expandParams', {
                        name,
                      })}
                      onClick={() =>
                        setExpansion({
                          sessionId: activeSessionId,
                          requestId: pending.requestId,
                          indexes: expanded
                            ? expandedIndexes.filter((item) => item !== index)
                            : [...expandedIndexes, index],
                        })
                      }
                    >
                      <ChevronRight size={14} aria-hidden="true" />
                    </button>
                    <span
                      hidden={!expanded}
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
                  </div>
                );
              })}
            </div>
            <p
              id={`${scopeId}-status`}
              role="status"
              data-testid="a4p-authorization-scope-status"
              className={`a4p-permission-status${selectedIndexes.length > 0 && !working ? ' sr-only' : ''}`}
            >
              {t(
                selectedIndexes.length === 0
                  ? 'a4pAuthorization.emptySelection'
                  : working
                    ? 'a4pAuthorization.waiting'
                    : 'a4pAuthorization.readyRange',
              )}
            </p>
          </fieldset>

          {error && (
            <div role="alert" data-testid="a4p-authorization-error" className="a4p-authorization-error">
              {error}
            </div>
          )}
        </div>

        <div className="a4p-authorization-footer" data-testid="a4p-authorization-footer">
          <span data-testid="a4p-authorization-selected-count" className="a4p-permission-count">
            {t('a4pAuthorization.selectedCount', { selected: selectedIndexes.length, total: candidates.length })}
          </span>
          <button
            type="button"
            className="a4p-authorization-button"
            data-testid="a4p-authorization-reject"
            onClick={reject}
            disabled={working}
          >
            {t('a4pAuthorization.reject')}
          </button>
          <button
            type="button"
            className="a4p-authorization-button a4p-authorization-button--approve"
            data-testid="a4p-authorization-approve"
            onClick={approve}
            disabled={working || selectedIndexes.length === 0}
          >
            {busy ? t('a4pAuthorization.waiting') : t('a4pAuthorization.approve')}
          </button>
        </div>
      </div>
    </div>
  );
}
