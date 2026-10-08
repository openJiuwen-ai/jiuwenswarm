import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import { shellRulePattern } from './shellRulePattern';

type Level = 'deny' | 'allow' | 'ask';
type Rule = { id?: string; tools: string[] | string; pattern: string; action?: Level; severity?: string; description?: string };
type Draft = { id?: string; mode: 'glob' | 'regex'; pattern: string; action: Level; description: string };
type RulesPayload = { rules: Rule[]; builtin_rules: Rule[] };
const levelOf = (rule: Rule): Level => rule.action || (rule.severity === 'LOW' || rule.severity === 'MEDIUM' ? 'allow' : 'ask');
const isRegex = (pattern: string) => pattern.toLowerCase().startsWith('re:');

export function ShellRulesEditor({ isConnected }: { isConnected: boolean }) {
  const { t } = useTranslation();
  const [data, setData] = useState<RulesPayload | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [reload, setReload] = useState(0);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const generation = useRef(0);
  const lock = useRef(false);
  const rulesList = useRef<HTMLDivElement>(null);
  const [rulesListHeight, setRulesListHeight] = useState<number>();
  const disabled = !isConnected || busy || loading;
  const inputClass = 'w-full min-w-0 rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent';

  useLayoutEffect(() => {
    const list = rulesList.current;
    if (!list || list.children.length <= 5) {
      setRulesListHeight(undefined);
      return;
    }
    const visibleRows = Array.from(list.children).slice(0, 5);
    const measure = () => {
      const first = visibleRows[0].getBoundingClientRect();
      const last = visibleRows[4].getBoundingClientRect();
      setRulesListHeight(Math.ceil(last.bottom - first.top));
    };
    measure();
    // Keep five rows visible when text wraps, the panel resizes, or a
    // delete confirmation changes a row's height.
    const observer = new ResizeObserver(measure);
    observer.observe(list);
    visibleRows.forEach(row => observer.observe(row));
    return () => observer.disconnect();
  }, [data?.rules, deleting]);

  useEffect(() => {
    const current = ++generation.current;
    if (!isConnected) return;
    setLoading(true);
    setError('');
    void webRequest<RulesPayload>('permissions.shell_guard.get', {})
      .then(result => {
        if (generation.current === current) setData(result);
      })
      .catch(e => {
        if (generation.current !== current) return;
        setData(null);
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (generation.current === current) setLoading(false);
      });
    return () => {
      generation.current++;
    };
  }, [isConnected, reload]);

  async function mutate(method: string, params: Record<string, unknown>, onSuccess: (result: { rule?: Rule }) => void) {
    if (disabled || lock.current) return;
    const current = generation.current;
    lock.current = true;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const result = await webRequest<{ rule?: Rule }>(method, params);
      if (generation.current !== current) return;
      onSuccess(result);
      setDraft(null);
      setDeleting(null);
      setMessage(t('shellSecurity.rulesSaved'));
    } catch (e) {
      if (generation.current === current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  function saveRule() {
    if (!draft) return;
    const validated = shellRulePattern(draft.mode, draft.pattern);
    if (validated.error) {
      setError(t(validated.error));
      return;
    }
    const patch = {
      pattern: validated.pattern,
      action: draft.action,
      description: draft.description.trim(),
    };
    const id = draft.id;
    void mutate(
      id ? 'permissions.rules.update' : 'permissions.rules.create',
      id ? { id, patch } : { rule: { ...patch, tools: ['shell'], match_type: 'command' } },
      result => {
        if (!result.rule) return;
        const rule = result.rule;
        setData(current => current && { ...current, rules: id ? current.rules.map(item => (item.id === id ? rule : item)) : [...current.rules, rule] });
      },
    );
  }

  const editor = draft ? (
    <fieldset disabled={disabled} className="rounded-lg border border-border p-4 disabled:opacity-60">
      <legend className="px-1 text-xs text-text-strong">{t(draft.id ? 'shellSecurity.editRule' : 'shellSecurity.addRule')}</legend>
      <div className="space-y-4">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <label className="block space-y-1.5 text-xs text-text-muted">
            <span className="block">{t('shellSecurity.listType')}</span>
            <select className={inputClass} value={draft.action} onChange={e => setDraft({ ...draft, action: e.target.value as Level })}>
              {(['deny', 'allow', 'ask'] as const).map(level => (
                <option key={level} value={level}>
                  {t(`shellSecurity.${level}`)}
                </option>
              ))}
            </select>
          </label>
          <label className="block space-y-1.5 text-xs text-text-muted">
            <span className="block">{t('shellSecurity.matchType')}</span>
            <select className={inputClass} value={draft.mode} onChange={e => setDraft({ ...draft, mode: e.target.value as Draft['mode'] })}>
              <option value="glob">{t('shellSecurity.glob')}</option>
              <option value="regex">{t('shellSecurity.regex')}</option>
            </select>
          </label>
        </div>
        <label className="block space-y-1.5 text-xs text-text-muted">
          <span className="block">{t('shellSecurity.pattern')}</span>
          <input
            autoFocus
            className={inputClass}
            value={draft.pattern}
            placeholder={draft.mode === 'regex' ? '^git\\s+status$' : 'git status *'}
            onChange={e => setDraft({ ...draft, pattern: e.target.value })}
          />
        </label>
        {draft.mode === 'regex' && <p className="text-xs text-text-muted">{t('shellSecurity.regexHint')}</p>}
        <label className="block space-y-1.5 text-xs text-text-muted">
          <span className="block">{t('shellSecurity.description')}</span>
          <input className={inputClass} value={draft.description} onChange={e => setDraft({ ...draft, description: e.target.value })} />
        </label>
        {error && (
          <p role="alert" className="text-xs text-danger">
            {error}
          </p>
        )}
        <div className="flex gap-2">
          <button type="button" className="btn text-xs" onClick={saveRule}>
            {t('shellSecurity.saveRule')}
          </button>
          <button
            type="button"
            className="btn text-xs"
            onClick={() => {
              setDraft(null);
              setError('');
            }}
          >
            {t('shellSecurity.cancel')}
          </button>
        </div>
      </div>
    </fieldset>
  ) : null;

  return (
    <div className="border-t border-border px-4 py-3 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-sm font-medium text-text-strong">{t('shellSecurity.userRules')}</h4>
        <div className="flex gap-2">
          <button type="button" className="btn text-xs disabled:opacity-50" disabled={disabled || !!draft || !!deleting} onClick={() => setReload(n => n + 1)}>
            {t('fileSecurity.refresh')}
          </button>
          <button
            type="button"
            className="btn text-xs disabled:opacity-50"
            disabled={disabled || !data || !!draft || !!deleting}
            onClick={() => {
              setDraft({ mode: 'glob', pattern: '', action: 'ask', description: '' });
              setMessage('');
              setError('');
            }}
          >
            {t('shellSecurity.addRule')}
          </button>
        </div>
      </div>
      <p className="text-xs text-text-muted">{t('shellSecurity.rulesHint')}</p>
      {loading && (
        <p role="status" className="text-xs text-text-muted">
          {t('fileSecurity.loading')}
        </p>
      )}
      {data?.rules.length === 0 && <p className="text-xs text-text-muted">{t('shellSecurity.noRules')}</p>}
      {!!data?.rules.length && (
        <div
          ref={rulesList}
          role="region"
          aria-label={t('shellSecurity.userRules')}
          tabIndex={data.rules.length > 5 ? 0 : undefined}
          className="flex flex-col gap-3 overflow-y-auto overscroll-contain"
          style={{ maxHeight: rulesListHeight }}
        >
          {data.rules.map((rule, index) => (
            <div key={rule.id || index} className="shrink-0 rounded-lg border border-border bg-secondary/20 p-3 space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                <span className="text-text-strong">
                  {t(`shellSecurity.${levelOf(rule)}`)} · {t(`shellSecurity.${isRegex(rule.pattern) ? 'regex' : 'glob'}`)}
                </span>
                <div className="flex gap-2">
                  <button
                    type="button"
                    className="btn text-xs disabled:opacity-50"
                    aria-expanded={!!draft?.id && draft.id === rule.id}
                    disabled={disabled || !rule.id || !!draft || !!deleting}
                    onClick={() => {
                      setDraft({
                        id: rule.id,
                        mode: isRegex(rule.pattern) ? 'regex' : 'glob',
                        pattern: isRegex(rule.pattern) ? rule.pattern.slice(3) : rule.pattern,
                        action: levelOf(rule),
                        description: rule.description || '',
                      });
                      setError('');
                      setMessage('');
                    }}
                  >
                    {t('shellSecurity.editRule')}
                  </button>
                  <button
                    type="button"
                    className="btn text-xs text-danger disabled:opacity-50"
                    disabled={disabled || !rule.id || !!draft || !!deleting}
                    onClick={() => setDeleting(rule.id!)}
                  >
                    {t('shellSecurity.deleteRule')}
                  </button>
                </div>
              </div>
              <code className="block break-all whitespace-pre-wrap text-xs text-text">{rule.pattern}</code>
              <p className="text-xs text-text-muted break-all">
                {t('shellSecurity.appliesTo')}: {Array.isArray(rule.tools) ? rule.tools.join(', ') : rule.tools}
              </p>
              {rule.description && <p className="text-xs text-text-muted break-words">{rule.description}</p>}
              {draft?.id && draft.id === rule.id && editor}
              {deleting === rule.id && (
                <div className="flex flex-wrap items-center gap-2 text-xs text-text">
                  <span>{t('shellSecurity.deleteConfirm')}</span>
                  <button
                    type="button"
                    className="btn text-xs text-danger"
                    disabled={disabled}
                    onClick={() =>
                      void mutate('permissions.rules.delete', { id: rule.id }, () =>
                        setData(current => current && { ...current, rules: current.rules.filter(item => item.id !== rule.id) }),
                      )
                    }
                  >
                    {t('shellSecurity.deleteRule')}
                  </button>
                  <button type="button" className="btn text-xs" disabled={disabled} onClick={() => setDeleting(null)}>
                    {t('shellSecurity.cancel')}
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      {draft && !draft.id && editor}
      {error && !draft && (
        <p role="alert" className="text-xs text-danger">
          {error}
        </p>
      )}
      {message && (
        <p role="status" className="text-xs text-text-muted">
          {message}
        </p>
      )}
      {data && (
        <details className="text-xs text-text-muted">
          <summary className="cursor-pointer py-2">
            {t('shellSecurity.builtinList')} ({data.builtin_rules.length})
          </summary>
          <div className="max-h-64 overflow-auto space-y-2">
            {data.builtin_rules.map((rule, index) => (
              <div key={rule.id || index} className="rounded-md border border-border p-2 space-y-1">
                <span className="text-text-strong">
                  {t(`shellSecurity.${levelOf(rule)}`)} · {rule.description || rule.id}
                </span>
                <code className="block break-all whitespace-pre-wrap">{rule.pattern}</code>
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}
