import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Check, ChevronLeft, Search, Trash2, Users, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Dialog } from '../../components/ui/Dialog/Dialog';
import { createAgentManagementClient } from '../agentManagement/client';
import { createAgentGroupManagementClient } from '../agentManagement/groupClient';
import type { AgentCatalogItem, AgentGroupCatalogItem } from '../agentManagement/types';
import { groupChatClient, type GroupInfo, type GroupMember } from './client';
import { groupMemberCandidates, newMember, type GroupMemberCandidate } from './state';

type Candidate = GroupMemberCandidate;

export function MemberPicker({ group, onClose, onSaved }: {
  group?: GroupInfo; onClose: () => void; onSaved: (group: GroupInfo) => void;
}) {
  const { t } = useTranslation();
  const titleId = useId();
  const [name, setName] = useState(t('groupChat.defaultName'));
  const [tab, setTab] = useState<GroupMember['kind']>('agent_template');
  const [query, setQuery] = useState('');
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [selected, setSelected] = useState<GroupMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [runtimeNotice, setRuntimeNotice] = useState<string | null>(null);
  const createAttempt = useRef<{ signature: string; token: string } | null>(null);
  const addTokens = useRef(new Map<string, string>());
  const [detail, setDetail] = useState<AgentGroupCatalogItem | null>(null);
  const [groups, setGroups] = useState<AgentGroupCatalogItem[]>([]);

  useEffect(() => {
    let active = true;
    async function load() {
      const results = await Promise.allSettled([
        createAgentManagementClient().listCatalog({ filter: 'mine', includeTeamCompatibility: true }),
        createAgentGroupManagementClient().listGroups({ filter: 'all' }),
      ]);
      if (!active) return;
      const agents: AgentCatalogItem[] = results[0].status === 'fulfilled' ? results[0].value : [];
      const teams: AgentGroupCatalogItem[] = results[1].status === 'fulfilled' ? results[1].value : [];
      setGroups(teams);
      setCandidates(groupMemberCandidates(agents, teams));
      const failure = results.find((result) => result.status === 'rejected');
      if (failure?.status === 'rejected') setError(String(failure.reason));
      setLoading(false);
    }
    void load();
    return () => { active = false; };
  }, []);

  const visible = useMemo(() => candidates.filter((item) => item.kind === tab
    && `${item.name} ${item.description}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())),
  [candidates, query, tab]);

  function toggle(item: Candidate) {
    const found = selected.find((member) => member.kind === item.kind && member.id === item.packageId);
    if (found) setSelected(selected.filter((member) => member !== found));
    else {
      setSelected([...selected, newMember(item.kind, item.packageId, item.name, item.description)]);
    }
  }

  async function save() {
    if (busy || !selected.length || (!group && !name.trim())) return;
    setBusy(true);
    setError('');
    try {
      if (group) {
        for (const member of selected) {
          let token = addTokens.current.get(member.member_name);
          if (!token) {
            token = crypto.randomUUID();
            addTokens.current.set(member.member_name, token);
          }
          await groupChatClient.addMember(group.group_id, member, token);
        }
        onSaved(await groupChatClient.get(group.group_id));
      } else {
        const signature = JSON.stringify({ name: name.trim(), members: selected });
        if (createAttempt.current?.signature !== signature) {
          createAttempt.current = { signature, token: crypto.randomUUID() };
        }
        onSaved(await groupChatClient.create({
          name: name.trim(), members: selected, create_token: createAttempt.current.token,
        }));
      }
    } catch (reason) {
      if (reason instanceof Error && 'code' in reason && reason.code === 'EXTERNAL_CLI_DISABLED') {
        const agents = reason.message === 'claude' ? 'Claude Code'
          : reason.message === 'codex' ? 'Codex' : reason.message;
        setRuntimeNotice(t('groupChat.externalRuntimeDisabled', { agents }));
      } else {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      setBusy(false);
    }
  }

  if (runtimeNotice !== null) {
    return (
      <Dialog open titleId={`${titleId}-runtime-notice`}
        className="group-chat-small-dialog" onCancel={() => setRuntimeNotice(null)}>
        <div data-testid="group-chat-runtime-notice">
          <header className="group-chat-dialog-header">
            <h2 id={`${titleId}-runtime-notice`} data-testid="group-chat-runtime-notice-title">
              {t('chat.agentGroupRuntimeDisabledTitle')}
            </h2>
          </header>
          <p data-testid="group-chat-runtime-notice-message">{runtimeNotice}</p>
          <footer className="group-chat-dialog-footer">
            <button type="button" className="btn primary" onClick={() => setRuntimeNotice(null)}
              data-testid="group-chat-runtime-notice-confirm">{t('chat.agentGroupRuntimeDisabledConfirm')}</button>
          </footer>
        </div>
      </Dialog>
    );
  }

  return (
    <Dialog open titleId={titleId} className="group-chat-picker" closeDisabled={busy} onCancel={onClose}>
      <div data-testid="group-chat-member-picker">
        <header className="group-chat-dialog-header">
          {detail ? <button type="button" className="group-chat-detail-back" onClick={() => setDetail(null)}
            data-testid="group-chat-detail-back"><ChevronLeft size={18} aria-hidden />{t('groupChat.back')}</button>
            : <h2 id={titleId} data-testid="group-chat-picker-title">
              {t(group ? 'groupChat.addMembers' : 'groupChat.create')}
            </h2>}
          <button type="button" disabled={busy} onClick={onClose} aria-label={t('common.close')}
            data-testid="group-chat-picker-close"><X size={18} /></button>
        </header>
        {!group && !detail && <label className="group-chat-name" data-testid="group-chat-name-label">
          {t('groupChat.name')}
          <input autoFocus value={name} maxLength={200} disabled={busy} onChange={(event) => setName(event.target.value)}
            data-testid="group-chat-name-input" />
        </label>}
        {error && <div role="alert" className="group-chat-error" data-testid="group-chat-picker-error">{error}</div>}
        {detail ? <section className="group-chat-catalog-detail" data-testid="group-chat-catalog-detail">
          <h3 id={titleId} data-testid="group-chat-detail-title">{detail.displayName}</h3>
          <p className="group-chat-detail-description" data-testid="group-chat-detail-description">{detail.description}</p>
          <h4 data-testid="group-chat-detail-members-title">
            {t('groupChat.teamMembers', { count: detail.members.length })}
          </h4>
          <div className="group-chat-candidates">
            {detail.members.map((member) => {
              const candidate = candidates.find((item) => item.kind === 'agent_template'
                && (item.id === member.agentTemplateId || item.packageId === member.agentTemplateId));
              return <article key={member.id} className="group-chat-candidate"
                data-testid="group-chat-detail-member" data-variant={member.id}>
                <div className="group-chat-detail-member-heading">
                  <span className="group-chat-avatar">{member.avatarUrl || candidate?.avatar
                    ? <img src={member.avatarUrl || candidate?.avatar || ''} alt="" /> : <Users size={20} />}</span>
                  <strong>{member.displayName}</strong>
                  {candidate?.providerLabel && <span className="group-chat-provider"
                    data-testid="group-chat-detail-member-provider">{candidate.providerLabel}</span>}
                </div>
                <p title={member.description}>{member.description}</p>
              </article>;
            })}
          </div>
          <h4 data-testid="group-chat-detail-skills-title">{t('groupChat.skills')}</h4>
          <div className="group-chat-candidates" data-testid="group-chat-detail-skills">
            {detail.skills.map((skill) => <article key={skill.id} className="group-chat-candidate"
              data-testid="group-chat-detail-skill" data-variant={skill.id}>
              <strong>{skill.name}</strong><p title={skill.description}>{skill.description}</p>
            </article>)}
          </div>
        </section> : <div className="group-chat-picker-body">
          <section className="group-chat-catalog" data-testid="group-chat-catalog">
            <div className="group-chat-tabs" role="tablist" aria-label={t('groupChat.members')}>
              {(['agent_template', 'agent_group'] as const).map((kind) => <button type="button" role="tab"
                key={kind} aria-selected={tab === kind} onClick={() => setTab(kind)} disabled={busy}
                data-testid="group-chat-catalog-tab" data-variant={kind}>
                {t(kind === 'agent_template' ? 'groupChat.experts' : 'groupChat.teams')}
              </button>)}
            </div>
            <label className="group-chat-search"><Search size={15} aria-hidden />
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t('groupChat.search')}
                aria-label={t('groupChat.search')} data-testid="group-chat-catalog-search" />
            </label>
            <div className="group-chat-candidates" data-testid="group-chat-candidate-list">
              {loading ? <p data-testid="group-chat-catalog-loading">{t('groupChat.loading')}</p> : null}
              {!loading && !visible.length && <p data-testid="group-chat-catalog-empty">{t('groupChat.noExperts')}</p>}
              {visible.map((item) => {
                const checked = selected.some((member) => member.id === item.packageId && member.kind === item.kind);
                const exists = group?.members.some((member) => member.id === item.packageId && member.kind === item.kind);
                return <div className={`group-chat-candidate${checked || exists ? ' is-selected' : ''}`} key={`${item.kind}:${item.id}`}
                  data-testid="group-chat-candidate" data-variant={item.id}>
                  <button type="button" onClick={() => toggle(item)} aria-pressed={checked || Boolean(exists)}
                    disabled={busy || exists} data-testid="group-chat-candidate-select">
                    <span className="group-chat-avatar">{item.avatar ? <img src={item.avatar} alt="" /> : <Users size={20} />}</span>
                    <strong>{item.name}</strong>
                    {item.providerLabel && <span className="group-chat-provider"
                      data-testid="group-chat-candidate-provider">{item.providerLabel}</span>}
                    <span className="group-chat-choice">{checked || exists ? <Check size={15} /> : null}</span>
                  </button>
                  {item.kind === 'agent_template' && item.description && <p className="group-chat-expert-description"
                    title={item.description} data-testid="group-chat-expert-description">{item.description}</p>}
                  {item.kind === 'agent_group' && <>
                    <p>{item.description}</p>
                    <button type="button" className="group-chat-text-button" onClick={() =>
                      setDetail(groups.find((team) => team.id === item.id) ?? null)}
                      data-testid="group-chat-team-detail">{t('groupChat.details')}</button>
                  </>}
                </div>;
              })}
            </div>
          </section>
          <aside className="group-chat-selected" data-testid="group-chat-selected-members">
            <h3 data-testid="group-chat-selected-title">{t('groupChat.selected', { count: selected.length })}</h3>
            {selected.map((member) => {
              const candidate = candidates.find((item) => item.kind === member.kind && item.packageId === member.id);
              return <div className="group-chat-member-row" key={member.member_name}
              data-testid="group-chat-selected-member" data-variant={member.member_name}>
              <span className="group-chat-avatar"><Users size={18} /></span>
              <span>{member.display_name}</span>
              {candidate?.providerLabel && <span className="group-chat-provider"
                data-testid="group-chat-selected-provider">{candidate.providerLabel}</span>}
              <button type="button" disabled={busy} aria-label={t('groupChat.removeSelection')}
                onClick={() => setSelected(selected.filter((item) => item !== member))}
                data-testid="group-chat-remove-selection"><Trash2 size={15} /></button>
            </div>;
            })}
          </aside>
        </div>}
        <footer className="group-chat-dialog-footer">
          {detail ? <button type="button" className="btn primary"
            disabled={busy || group?.members.some((member) => member.kind === 'agent_group'
              && member.id === (detail.name.trim() || detail.id))}
            onClick={() => {
              const candidate = candidates.find((item) => item.kind === 'agent_group' && item.id === detail.id);
              if (candidate && !selected.some((member) => member.kind === candidate.kind
                && member.id === candidate.packageId)) toggle(candidate);
              setDetail(null);
            }} data-testid="group-chat-use-team">{t('groupChat.useTeam')}</button> : <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}
            data-testid="group-chat-picker-cancel">{t('common.cancel')}</button>
          <button type="button" className="btn primary" onClick={() => void save()}
            disabled={busy || !selected.length || (!group && !name.trim())} data-testid="group-chat-picker-save">
            {t(busy ? 'groupChat.saving' : 'common.confirm')}
          </button>
          </>}
        </footer>
      </div>
    </Dialog>
  );
}
