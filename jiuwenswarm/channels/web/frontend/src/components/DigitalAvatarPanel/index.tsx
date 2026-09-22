import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, RefreshCw } from 'lucide-react';
import { webRequest } from '../../services/webClient';
import './DigitalAvatarPanel.css';

type ChannelId = 'feishu' | 'dingtalk' | 'welink';
type KindFilter = 'all' | 'group' | 'user' | 'hosted';

const CHANNEL_STORAGE_KEY = 'jiuwenswarm.digitalAvatar.channelId';

function readLastChannel(): ChannelId {
  try {
    const raw = window.localStorage.getItem(CHANNEL_STORAGE_KEY);
    if (raw === 'feishu' || raw === 'dingtalk' || raw === 'welink') return raw;
  } catch {
    /* private mode */
  }
  return 'feishu';
}

function writeLastChannel(id: ChannelId) {
  try {
    window.localStorage.setItem(CHANNEL_STORAGE_KEY, id);
  } catch {
    /* private mode */
  }
}

const CHANNEL_ORDER: ChannelId[] = ['feishu', 'dingtalk', 'welink'];
const FALLBACK_LABEL: Record<ChannelId, string> = {
  feishu: '飞书',
  dingtalk: '钉钉',
  welink: 'WeLink',
};

type ChannelStatus = {
  id: ChannelId;
  label: string;
  cli_available: boolean;
  message?: string;
};

type HostingRule = {
  match_mode: 'keyword' | 'relevant';
  keywords: string[];
  strip_keywords?: boolean;
};

type HostedTarget = {
  id: string;
  channel_id: ChannelId;
  target_kind: 'group' | 'user';
  external_id: string;
  title?: string;
  enabled: boolean;
  source?: 'manual' | 'auto' | string;
  last_poll_at_ms?: number | null;
  last_error?: string | null;
  rule_override?: HostingRule | null;
  expert_service_id?: string | null;
  expert_agent_id?: string | null;
  expert_persona?: string | null;
};

type ChannelPolicy = {
  auto_host_groups?: boolean;
  auto_host_users?: boolean;
  auto_host_max_new?: number;
  discover_interval_seconds?: number;
  default_group_rule?: HostingRule;
  default_user_rule?: HostingRule;
  expert_persona?: string;
};

/** 个人版尚无专家选择，租户固定 default/default；UI 称「分身」。企业接入专家后再露出选择器。 */
const PERSONAL_EXPERT = { serviceId: 'default', agentId: 'default' };

function avatarProfileDraft(
  target: HostedTarget | null | undefined,
  fallback: string,
): string {
  const saved = target?.expert_persona;
  if (saved && saved.trim()) return saved;
  return fallback;
}

function targetInheritsGlobal(target?: HostedTarget | null): boolean {
  if (!target) return false;
  const override = target.rule_override;
  const hasRule = Boolean(override?.match_mode || (override?.keywords && override.keywords.length));
  return !hasRule && !String(target.expert_persona || '').trim();
}

function globalButtonLabel(
  t: (key: string) => string,
  policy?: ChannelPolicy | null,
): string {
  const users = Boolean(policy?.auto_host_users);
  const groups = Boolean(policy?.auto_host_groups);
  if (!users && !groups) return t('digitalAvatar.globalButton');
  if (users && !groups) return t('digitalAvatar.globalButtonOnUsers');
  if (groups && !users) return t('digitalAvatar.globalButtonOnGroups');
  return t('digitalAvatar.globalButtonOn');
}

type DiscoverItem = {
  channel_id: ChannelId;
  target_kind: 'group' | 'user';
  external_id: string;
  title?: string | null;
  already_hosted: boolean;
};

type SessionRow = {
  key: string;
  channel_id: ChannelId;
  target_kind: 'group' | 'user';
  external_id: string;
  title: string;
  hosted: boolean;
  target?: HostedTarget;
};

type HistoryRecord = {
  role?: string;
  event_type?: string;
  content?: string;
  reasoning_content?: string;
  tool_call?: { name?: string; arguments?: unknown };
  tool_result?: { content?: string; output?: string };
  timestamp?: string | number;
};

type HistoryLine = {
  key: string;
  kind: 'inbound' | 'reply';
  text: string;
  reasoning?: string;
  tools?: string[];
};

function historyLines(records: HistoryRecord[]): HistoryLine[] {
  const out: HistoryLine[] = [];
  let pendingReasoning = '';
  const pendingTools: string[] = [];

  const takeProcess = () => {
    const reasoning = pendingReasoning;
    const tools = pendingTools.splice(0, pendingTools.length);
    pendingReasoning = '';
    return { reasoning: reasoning || undefined, tools: tools.length ? tools : undefined };
  };

  records.forEach((record, index) => {
    const role = String(record.role || '');
    const event = String(record.event_type || '');
    const content = typeof record.content === 'string' ? record.content.trim() : '';
    const reasoning = typeof record.reasoning_content === 'string' ? record.reasoning_content.trim() : '';
    if (role === 'user' || role === 'human') {
      if (content) out.push({ key: `u-${index}`, kind: 'inbound', text: content });
      return;
    }
    if (event === 'chat.reasoning' && (content || reasoning)) {
      pendingReasoning = [pendingReasoning, reasoning || content].filter(Boolean).join('\n\n');
      return;
    }
    if (event === 'chat.tool_call') {
      const name = record.tool_call?.name || 'tool';
      const rawArgs = record.tool_call?.arguments;
      const args = rawArgs == null ? '' : typeof rawArgs === 'string' ? rawArgs : JSON.stringify(rawArgs);
      pendingTools.push(args ? `${name} ${args}` : name);
      return;
    }
    if (event === 'chat.tool_result') {
      const text = String(record.tool_result?.content || record.tool_result?.output || content || '').trim();
      if (text) pendingTools.push(text);
      return;
    }
    if (reasoning) {
      pendingReasoning = [pendingReasoning, reasoning].filter(Boolean).join('\n\n');
    }
    if (content && (event === 'chat.final' || event === '' || role === 'assistant')) {
      out.push({ key: `a-${index}`, kind: 'reply', text: content, ...takeProcess() });
    }
  });
  if (pendingReasoning || pendingTools.length) {
    const last = out[out.length - 1];
    if (last?.kind === 'reply') {
      const leftover = takeProcess();
      last.reasoning = [last.reasoning, leftover.reasoning].filter(Boolean).join('\n\n') || undefined;
      last.tools = [...(last.tools || []), ...(leftover.tools || [])];
      if (!last.tools.length) last.tools = undefined;
    }
  }
  return out;
}

interface DigitalAvatarPanelProps {
  isConnected: boolean;
}

function itemKey(item: { target_kind: string; external_id: string }) {
  return `${item.target_kind}:${item.external_id}`;
}

function keywordsToText(keywords?: string[]) {
  return (keywords || []).join('，');
}

function textToKeywords(value: string) {
  return value
    .split(/[,，\n;；]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function defaultMode(kind: SessionRow['target_kind']): HostingRule['match_mode'] {
  return kind === 'user' ? 'relevant' : 'keyword';
}

export function DigitalAvatarPanel({ isConnected }: DigitalAvatarPanelProps) {
  const { t } = useTranslation();
  const [channelId, setChannelId] = useState<ChannelId>(readLastChannel);
  const [channels, setChannels] = useState<ChannelStatus[]>([]);
  const [targets, setTargets] = useState<HostedTarget[]>([]);
  const [discoverItems, setDiscoverItems] = useState<DiscoverItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [kindFilter, setKindFilter] = useState<KindFilter>('all');
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [draftMode, setDraftMode] = useState<HostingRule['match_mode']>('keyword');
  const [draftKeywords, setDraftKeywords] = useState('');
  const [draftPersona, setDraftPersona] = useState('');
  const [inheritGlobal, setInheritGlobal] = useState(false);
  const [saving, setSaving] = useState(false);
  const [historyRecords, setHistoryRecords] = useState<HistoryRecord[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [channelPolicy, setChannelPolicy] = useState<ChannelPolicy | null>(null);
  const [ruleOpen, setRuleOpen] = useState(false);
  const [ruleKey, setRuleKey] = useState<string | null>(null);
  const [globalOpen, setGlobalOpen] = useState(false);
  const [globalSaving, setGlobalSaving] = useState(false);
  const [gUsers, setGUsers] = useState(false);
  const [gGroups, setGGroups] = useState(false);
  const [gMaxNew, setGMaxNew] = useState(20);
  const [gMinutes, setGMinutes] = useState(5);
  const [gUserMode, setGUserMode] = useState<HostingRule['match_mode']>('relevant');
  const [gUserKeywords, setGUserKeywords] = useState('');
  const [gGroupMode, setGGroupMode] = useState<HostingRule['match_mode']>('keyword');
  const [gGroupKeywords, setGGroupKeywords] = useState('');
  const [gPersona, setGPersona] = useState('');

  const currentChannel = channels.find((item) => item.id === channelId);
  const channelOptions = CHANNEL_ORDER.map((id) => {
    const ch = channels.find((item) => item.id === id);
    return {
      id,
      label: ch?.label || FALLBACK_LABEL[id],
      cli_available: Boolean(ch?.cli_available),
    };
  });

  const refresh = useCallback(async () => {
    if (!isConnected) return;
    setLoading(true);
    setError(null);
    try {
      const [status, listed, discovered, policyPayload] = await Promise.all([
        webRequest<{ channels?: ChannelStatus[] }>('im.hosting.status'),
        webRequest<{ targets?: HostedTarget[] }>('im.hosting.targets.list', { channel_id: channelId }),
        webRequest<{ conversations?: DiscoverItem[] }>(
          'im.hosting.discover',
          { channel_id: channelId },
          { timeoutMs: 60000 },
        ),
        webRequest<{ policy?: Record<ChannelId, ChannelPolicy> }>('im.hosting.policy.get'),
      ]);
      setChannels(status.channels || []);
      setTargets(listed.targets || []);
      setDiscoverItems(discovered.conversations || []);
      setChannelPolicy(policyPayload.policy?.[channelId] || null);
    } catch (err) {
      setDiscoverItems([]);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [channelId, isConnected]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    writeLastChannel(channelId);
  }, [channelId]);

  const rows = useMemo(() => {
    const hostedByKey = new Map(targets.map((item) => [itemKey(item), item]));
    const seen = new Set<string>();
    const out: SessionRow[] = [];

    for (const item of discoverItems) {
      const key = itemKey(item);
      seen.add(key);
      const target = hostedByKey.get(key);
      out.push({
        key,
        channel_id: item.channel_id,
        target_kind: item.target_kind,
        external_id: item.external_id,
        title: item.title || item.external_id,
        hosted: Boolean(target?.enabled),
        target,
      });
    }

    for (const target of targets) {
      const key = itemKey(target);
      if (seen.has(key)) continue;
      out.push({
        key,
        channel_id: target.channel_id,
        target_kind: target.target_kind,
        external_id: target.external_id,
        title: target.title || target.external_id,
        hosted: Boolean(target.enabled),
        target,
      });
    }

    return out.filter((row) => {
      if (kindFilter === 'hosted') return row.hosted;
      if (kindFilter === 'group' || kindFilter === 'user') return row.target_kind === kindFilter;
      return true;
    });
  }, [discoverItems, kindFilter, targets]);

  const selectedRow = rows.find((row) => row.key === selectedKey) || null;
  const ruleRow = rows.find((row) => row.key === ruleKey) || null;
  const selectedTargetId = selectedRow?.target?.id || null;
  const timeline = useMemo(() => historyLines(historyRecords), [historyRecords]);

  const loadHistory = useCallback(async (targetId: string | null) => {
    if (!isConnected || !targetId) {
      setHistoryRecords([]);
      return;
    }
    setHistoryLoading(true);
    try {
      const payload = await webRequest<{ messages?: HistoryRecord[] }>('im.hosting.history', { id: targetId });
      setHistoryRecords(payload.messages || []);
    } catch {
      setHistoryRecords([]);
    } finally {
      setHistoryLoading(false);
    }
  }, [isConnected]);

  useEffect(() => {
    void loadHistory(selectedTargetId);
  }, [loadHistory, selectedTargetId]);

  useEffect(() => {
    setSelectedKey(null);
    setRuleOpen(false);
    setRuleKey(null);
  }, [channelId]);

  const inheritedRule = (kind: SessionRow['target_kind']): HostingRule => {
    const raw = kind === 'user' ? channelPolicy?.default_user_rule : channelPolicy?.default_group_rule;
    return {
      match_mode: raw?.match_mode || defaultMode(kind),
      keywords: raw?.keywords || [],
    };
  };

  const fillSessionDraft = (row: SessionRow) => {
    const inherit = targetInheritsGlobal(row.target);
    setInheritGlobal(inherit);
    if (inherit) {
      const rule = inheritedRule(row.target_kind);
      setDraftMode(rule.match_mode);
      setDraftKeywords(keywordsToText(rule.keywords));
      setDraftPersona(
        (channelPolicy?.expert_persona || '').trim() || t('digitalAvatar.avatarProfileTemplate'),
      );
      return;
    }
    const override = row.target?.rule_override;
    setDraftMode(override?.match_mode || defaultMode(row.target_kind));
    setDraftKeywords(keywordsToText(override?.keywords));
    setDraftPersona(
      avatarProfileDraft(
        row.target,
        (channelPolicy?.expert_persona || '').trim() || t('digitalAvatar.avatarProfileTemplate'),
      ),
    );
  };

  useEffect(() => {
    if (!ruleOpen || !ruleRow) return;
    fillSessionDraft(ruleRow);
  }, [ruleOpen, ruleRow?.key, ruleRow?.target?.id, ruleRow?.target_kind, channelPolicy, t]);

  const openChat = (row: SessionRow) => {
    setSelectedKey(row.key);
  };

  const openRuleModal = (row: SessionRow) => {
    setRuleKey(row.key);
    fillSessionDraft(row);
    setRuleOpen(true);
  };

  const closeRuleModal = () => {
    setRuleOpen(false);
    setRuleKey(null);
  };

  const applyInherit = (next: boolean) => {
    setInheritGlobal(next);
    if (!ruleRow || !next) return;
    const rule = inheritedRule(ruleRow.target_kind);
    setDraftMode(rule.match_mode);
    setDraftKeywords(keywordsToText(rule.keywords));
    setDraftPersona(
      (channelPolicy?.expert_persona || '').trim() || t('digitalAvatar.avatarProfileTemplate'),
    );
  };

  const openGlobalDialog = () => {
    const policy = channelPolicy;
    setGUsers(Boolean(policy?.auto_host_users));
    setGGroups(Boolean(policy?.auto_host_groups));
    setGMaxNew(Number(policy?.auto_host_max_new || 20));
    setGMinutes(Math.max(1, Math.round(Number(policy?.discover_interval_seconds || 300) / 60)));
    setGUserMode(policy?.default_user_rule?.match_mode || 'relevant');
    setGUserKeywords(keywordsToText(policy?.default_user_rule?.keywords));
    setGGroupMode(policy?.default_group_rule?.match_mode || 'keyword');
    setGGroupKeywords(keywordsToText(policy?.default_group_rule?.keywords));
    setGPersona((policy?.expert_persona || '').trim() || t('digitalAvatar.avatarProfileTemplate'));
    setGlobalOpen(true);
  };

  const saveGlobalPolicy = async () => {
    const userKw = textToKeywords(gUserKeywords);
    const groupKw = textToKeywords(gGroupKeywords);
    setGlobalSaving(true);
    setError(null);
    try {
      const payload = await webRequest<{ policy?: Record<ChannelId, ChannelPolicy> }>(
        'im.hosting.policy.patch',
        {
          channel_id: channelId,
          auto_host_users: gUsers,
          auto_host_groups: gGroups,
          auto_host_max_new: Math.max(1, Math.min(50, Number(gMaxNew) || 20)),
          discover_interval_seconds: Math.max(1, Number(gMinutes) || 5) * 60,
          default_user_rule: { match_mode: gUserMode, keywords: userKw },
          default_group_rule: { match_mode: gGroupMode, keywords: groupKw },
          expert_persona: gPersona.trim(),
        },
      );
      setChannelPolicy(payload.policy?.[channelId] || null);
      setGlobalOpen(false);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setGlobalSaving(false);
    }
  };

  const currentRule = (): HostingRule => ({
    match_mode: draftMode,
    keywords: textToKeywords(draftKeywords),
  });

  const confirmHost = async () => {
    if (!ruleRow) return;
    const rule = currentRule();
    setSaving(true);
    setError(null);
    try {
      await webRequest('im.hosting.targets.add', {
        channel_id: ruleRow.channel_id,
        target_kind: ruleRow.target_kind,
        external_id: ruleRow.external_id,
        title: ruleRow.title,
        rule_override: inheritGlobal ? null : rule,
        expert_service_id: PERSONAL_EXPERT.serviceId,
        expert_agent_id: PERSONAL_EXPERT.agentId,
        expert_persona: inheritGlobal ? '' : draftPersona.trim(),
      });
      setSelectedKey(ruleRow.key);
      closeRuleModal();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const saveHostedRule = async () => {
    if (!ruleRow?.target) return;
    const rule = currentRule();
    setSaving(true);
    setError(null);
    try {
      await webRequest('im.hosting.targets.patch', {
        id: ruleRow.target.id,
        rule_override: inheritGlobal ? null : rule,
        expert_service_id: PERSONAL_EXPERT.serviceId,
        expert_agent_id: PERSONAL_EXPERT.agentId,
        expert_persona: inheritGlobal ? '' : draftPersona.trim(),
      });
      closeRuleModal();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const unhostRow = async (row: SessionRow) => {
    if (!row.target) return;
    setBusyKey(row.key);
    setError(null);
    try {
      await webRequest('im.hosting.targets.delete', { id: row.target.id });
      if (ruleKey === row.key) closeRuleModal();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyKey(null);
    }
  };

  const channelLabel = currentChannel?.label || FALLBACK_LABEL[channelId];

  return (
    <div className="da-panel">
      <div className="card da-panel__card">
        <header className="da-panel__header">
          <div>
            <h1>{t('digitalAvatar.title')}</h1>
            <p>{t('digitalAvatar.subtitle')}</p>
          </div>
        </header>

        {!isConnected ? <div className="da-panel__alert">{t('digitalAvatar.disconnected')}</div> : null}
        {error ? <div className="da-panel__alert da-panel__alert--error">{error}</div> : null}

        <div className="da-panel__toolbar">
          <label className="da-panel__channel">
            <span>{t('digitalAvatar.currentChannel')}</span>
            <span className="da-panel__select">
              <select value={channelId} onChange={(event) => setChannelId(event.target.value as ChannelId)}>
                {channelOptions.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.cli_available
                      ? item.label
                      : t('digitalAvatar.channelUnavailable', { label: item.label })}
                  </option>
                ))}
              </select>
              <ChevronDown size={14} aria-hidden />
            </span>
          </label>

          <button
            type="button"
            className={`da-panel__global-btn${channelPolicy?.auto_host_users || channelPolicy?.auto_host_groups ? ' is-on' : ''}`}
            onClick={openGlobalDialog}
            disabled={!isConnected}
          >
            {globalButtonLabel(t, channelPolicy)}
          </button>

          <div className="da-panel__pills" role="tablist" aria-label={t('digitalAvatar.filterLabel')}>
            {(['all', 'group', 'user', 'hosted'] as const).map((key) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={kindFilter === key}
                className={kindFilter === key ? 'is-active' : ''}
                onClick={() => setKindFilter(key)}
              >
                {t(`digitalAvatar.filter.${key}`)}
              </button>
            ))}
          </div>
        </div>

        <div className="da-panel__grid">
          <section className="da-panel__tile">
            <div className="da-panel__tile-head">
              <div>
                <h2>{t('digitalAvatar.listTitle')}</h2>
                <p>{t('digitalAvatar.listMeta', { channel: channelLabel, count: rows.length })}</p>
              </div>
              <button
                type="button"
                className="da-panel__icon-btn"
                onClick={() => void refresh()}
                disabled={!isConnected || loading}
              >
                <RefreshCw size={14} />
                {loading ? t('common.refreshing') : t('common.refresh')}
              </button>
            </div>
            <div className="da-panel__tile-body">
              {loading && rows.length === 0 ? (
                <p className="da-panel__empty">{t('digitalAvatar.recentLoading')}</p>
              ) : rows.length === 0 ? (
                <p className="da-panel__empty">
                  {kindFilter === 'hosted' ? t('digitalAvatar.emptyHosted') : t('digitalAvatar.recentEmpty')}
                </p>
              ) : (
                <ul className="da-panel__list">
                  {rows.map((row) => (
                    <li
                      key={row.key}
                      className={`da-panel__row is-clickable${selectedKey === row.key ? ' is-selected' : ''}`}
                      onClick={() => openChat(row)}
                    >
                      <div className="da-panel__row-main">
                        <span className="da-panel__name">{row.title}</span>
                        <span className={`da-panel__tag${row.target_kind === 'group' ? ' is-group' : ''}`}>
                          {row.target_kind === 'group' ? t('digitalAvatar.group') : t('digitalAvatar.user')}
                        </span>
                        {row.hosted ? (
                          <span className="da-panel__tag is-hosted">{t('digitalAvatar.hosted')}</span>
                        ) : null}
                        {row.target?.source === 'auto' ? (
                          <span className="da-panel__tag is-auto">{t('digitalAvatar.sourceAuto')}</span>
                        ) : null}
                      </div>
                      <div className="da-panel__row-actions">
                        {row.hosted ? (
                          <>
                            <button
                              type="button"
                              className="da-panel__text-btn"
                              disabled={!isConnected}
                              onClick={(event) => {
                                event.stopPropagation();
                                openRuleModal(row);
                              }}
                            >
                              {t('digitalAvatar.editRules')}
                            </button>
                            <button
                              type="button"
                              className="da-panel__text-btn da-panel__text-btn--danger"
                              disabled={busyKey === row.key}
                              onClick={(event) => {
                                event.stopPropagation();
                                void unhostRow(row);
                              }}
                            >
                              {t('digitalAvatar.unhost')}
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            className="da-panel__host-btn"
                            disabled={!isConnected}
                            onClick={(event) => {
                              event.stopPropagation();
                              openRuleModal(row);
                            }}
                          >
                            {t('digitalAvatar.host')}
                          </button>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </section>

          <section className="da-panel__tile">
            {selectedRow ? (
              <>
                <div className="da-panel__tile-head">
                  <div>
                    <h2>{selectedRow.title}</h2>
                    <p>{t('digitalAvatar.historyHint')}</p>
                  </div>
                  <div className="da-panel__row-actions">
                    <button
                      type="button"
                      className="da-panel__text-btn"
                      disabled={!isConnected}
                      onClick={() => openRuleModal(selectedRow)}
                    >
                      {selectedRow.hosted ? t('digitalAvatar.editRules') : t('digitalAvatar.host')}
                    </button>
                    {selectedRow.hosted ? (
                      <button
                        type="button"
                        className="da-panel__icon-btn"
                        onClick={() => void loadHistory(selectedTargetId)}
                        disabled={!isConnected || historyLoading}
                      >
                        <RefreshCw size={14} />
                        {historyLoading ? t('common.refreshing') : t('common.refresh')}
                      </button>
                    ) : null}
                  </div>
                </div>
                <div className="da-panel__tile-body da-panel__chat">
                  {!selectedRow.hosted ? (
                    <p className="da-panel__empty">{t('digitalAvatar.historyNeedHost')}</p>
                  ) : historyLoading && timeline.length === 0 ? (
                    <p className="da-panel__empty">{t('digitalAvatar.historyLoading')}</p>
                  ) : timeline.length === 0 ? (
                    <p className="da-panel__empty">{t('digitalAvatar.historyEmpty')}</p>
                  ) : (
                    <ol className="da-panel__timeline">
                      {timeline.map((item) => (
                        <li key={item.key} className={`da-panel__turn is-${item.kind}`}>
                          {item.kind === 'reply' && (item.reasoning || item.tools?.length) ? (
                            <details className="da-panel__think">
                              <summary>{t('digitalAvatar.reasoningToggle')}</summary>
                              {item.reasoning ? (
                                <div className="da-panel__think-body">{item.reasoning}</div>
                              ) : null}
                              {item.tools?.map((tool, index) => (
                                <div key={`${item.key}-tool-${index}`} className="da-panel__think-body is-tool">
                                  {tool}
                                </div>
                              ))}
                            </details>
                          ) : null}
                          <div className="da-panel__turn-text">{item.text}</div>
                        </li>
                      ))}
                    </ol>
                  )}
                </div>
              </>
            ) : (
              <div className="da-panel__tile-body da-panel__placeholder">
                <p>{t('digitalAvatar.pickToConfigure')}</p>
              </div>
            )}
          </section>
        </div>
      </div>

      {ruleOpen && ruleRow ? (
        <div className="da-panel__modal" role="dialog" aria-modal="true" aria-labelledby="da-rule-title">
          <button type="button" className="da-panel__modal-backdrop" aria-label={t('common.close')} onClick={closeRuleModal} />
          <div className="da-panel__modal-card da-panel__modal-card--wide">
            <div className="da-panel__tile-head">
              <div>
                <h2 id="da-rule-title">
                  {ruleRow.title}
                  {ruleRow.target_kind === 'group' ? ` · ${t('digitalAvatar.group')}` : ` · ${t('digitalAvatar.user')}`}
                </h2>
                <p>
                  {ruleRow.hosted ? t('digitalAvatar.editHostedHint') : t('digitalAvatar.draftHostHint')}
                </p>
              </div>
              <button type="button" className="da-panel__text-btn" onClick={closeRuleModal}>
                {t('common.close')}
              </button>
            </div>
            <div className="da-panel__modal-body">
              <label className="da-panel__check">
                <input
                  type="checkbox"
                  checked={inheritGlobal}
                  onChange={(event) => applyInherit(event.target.checked)}
                />
                <span>{t('digitalAvatar.inheritGlobal')}</span>
              </label>
              <p className="da-panel__note">{t('digitalAvatar.inheritGlobalHint')}</p>
              <details className="da-panel__fold" open={!inheritGlobal}>
                <summary>{t('digitalAvatar.gateSection')}</summary>
                <p className="da-panel__note">{t('digitalAvatar.gateHint')}</p>
                <label className="da-panel__field">
                  <span>{t('digitalAvatar.matchMode')}</span>
                  <select
                    value={draftMode}
                    onChange={(event) => setDraftMode(event.target.value as HostingRule['match_mode'])}
                    disabled={inheritGlobal}
                  >
                    <option value="keyword">{t('digitalAvatar.modeKeyword')}</option>
                    <option value="relevant">{t('digitalAvatar.modeRelevant')}</option>
                  </select>
                </label>
                <label className="da-panel__field">
                  <span>{t('digitalAvatar.keywords')}</span>
                  <textarea
                    value={draftKeywords}
                    onChange={(event) => setDraftKeywords(event.target.value)}
                    placeholder={t('digitalAvatar.keywordsPlaceholder')}
                    disabled={inheritGlobal}
                    rows={3}
                  />
                </label>
              </details>
              <details className="da-panel__fold" open={!inheritGlobal}>
                <summary>{t('digitalAvatar.avatarSection')}</summary>
                <p className="da-panel__note">{t('digitalAvatar.avatarHint')}</p>
                <label className="da-panel__field">
                  <span>{t('digitalAvatar.avatarProfile')}</span>
                  <textarea
                    className="da-panel__profile"
                    value={draftPersona}
                    onChange={(event) => setDraftPersona(event.target.value)}
                    placeholder={t('digitalAvatar.avatarProfileTemplate')}
                    disabled={inheritGlobal}
                    rows={10}
                    spellCheck={false}
                  />
                </label>
              </details>
              <div className="da-panel__actions da-panel__modal-actions">
                {ruleRow.hosted ? (
                  <button
                    type="button"
                    className="da-panel__host-btn"
                    onClick={() => void saveHostedRule()}
                    disabled={!isConnected || saving}
                  >
                    {saving ? t('common.saving') : t('digitalAvatar.saveSession')}
                  </button>
                ) : (
                  <button
                    type="button"
                    className="da-panel__host-btn"
                    onClick={() => void confirmHost()}
                    disabled={!isConnected || saving}
                  >
                    {saving ? t('common.saving') : t('digitalAvatar.confirmHost')}
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {globalOpen ? (
        <div className="da-panel__modal" role="dialog" aria-modal="true" aria-labelledby="da-global-title">
          <button type="button" className="da-panel__modal-backdrop" aria-label={t('common.close')} onClick={() => setGlobalOpen(false)} />
          <div className="da-panel__modal-card">
            <div className="da-panel__tile-head">
              <div>
                <h2 id="da-global-title">{t('digitalAvatar.globalDialogTitle')}</h2>
                <p>{t('digitalAvatar.globalDialogHint')}</p>
              </div>
              <button type="button" className="da-panel__text-btn" onClick={() => setGlobalOpen(false)}>
                {t('common.close')}
              </button>
            </div>
            <div className="da-panel__modal-body">
              <label className="da-panel__check">
                <input type="checkbox" checked={gUsers} onChange={(event) => setGUsers(event.target.checked)} />
                <span>{t('digitalAvatar.globalAutoUsers')}</span>
              </label>
              <label className="da-panel__check">
                <input type="checkbox" checked={gGroups} onChange={(event) => setGGroups(event.target.checked)} />
                <span>{t('digitalAvatar.globalAutoGroups')}</span>
              </label>
              {gUsers || gGroups ? <p className="da-panel__note">{t('digitalAvatar.globalRisk')}</p> : null}

              <div className="da-panel__modal-grid">
                <label className="da-panel__field">
                  <span>{t('digitalAvatar.globalMaxNew')}</span>
                  <input
                    type="number"
                    min={1}
                    max={50}
                    value={gMaxNew}
                    onChange={(event) => setGMaxNew(Number(event.target.value))}
                  />
                </label>
                <label className="da-panel__field">
                  <span>{t('digitalAvatar.globalDiscoverMinutes')}</span>
                  <input
                    type="number"
                    min={1}
                    max={180}
                    value={gMinutes}
                    onChange={(event) => setGMinutes(Number(event.target.value))}
                  />
                </label>
              </div>

              <div className="da-panel__rule-block">
                <details className="da-panel__fold" open>
                  <summary>{t('digitalAvatar.userDefault')}</summary>
                  <label className="da-panel__field">
                    <span>{t('digitalAvatar.matchMode')}</span>
                    <select
                      value={gUserMode}
                      onChange={(event) => setGUserMode(event.target.value as HostingRule['match_mode'])}
                    >
                      <option value="keyword">{t('digitalAvatar.modeKeyword')}</option>
                      <option value="relevant">{t('digitalAvatar.modeRelevant')}</option>
                    </select>
                  </label>
                  <label className="da-panel__field">
                    <span>{t('digitalAvatar.keywords')}</span>
                    <textarea
                      value={gUserKeywords}
                      onChange={(event) => setGUserKeywords(event.target.value)}
                      placeholder={t('digitalAvatar.keywordsPlaceholder')}
                      rows={3}
                    />
                  </label>
                </details>
                <details className="da-panel__fold">
                  <summary>{t('digitalAvatar.groupDefault')}</summary>
                  <label className="da-panel__field">
                    <span>{t('digitalAvatar.matchMode')}</span>
                    <select
                      value={gGroupMode}
                      onChange={(event) => setGGroupMode(event.target.value as HostingRule['match_mode'])}
                    >
                      <option value="keyword">{t('digitalAvatar.modeKeyword')}</option>
                      <option value="relevant">{t('digitalAvatar.modeRelevant')}</option>
                    </select>
                  </label>
                  <label className="da-panel__field">
                    <span>{t('digitalAvatar.keywords')}</span>
                    <textarea
                      value={gGroupKeywords}
                      onChange={(event) => setGGroupKeywords(event.target.value)}
                      placeholder={t('digitalAvatar.keywordsPlaceholder')}
                      rows={3}
                    />
                  </label>
                </details>
                <details className="da-panel__fold">
                  <summary>{t('digitalAvatar.avatarSection')}</summary>
                  <p className="da-panel__note">{t('digitalAvatar.avatarHint')}</p>
                  <label className="da-panel__field">
                    <span>{t('digitalAvatar.avatarProfile')}</span>
                    <textarea
                      className="da-panel__profile"
                      value={gPersona}
                      onChange={(event) => setGPersona(event.target.value)}
                      placeholder={t('digitalAvatar.avatarProfileTemplate')}
                      rows={10}
                      spellCheck={false}
                    />
                  </label>
                </details>
              </div>

              <div className="da-panel__actions">
                <button
                  type="button"
                  className="da-panel__host-btn"
                  disabled={!isConnected || globalSaving}
                  onClick={() => void saveGlobalPolicy()}
                >
                  {globalSaving ? t('common.saving') : t('digitalAvatar.globalSave')}
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
