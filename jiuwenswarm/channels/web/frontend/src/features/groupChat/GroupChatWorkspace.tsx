import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AtSign, Bot, ChevronLeft, Folder, MessagesSquare, PencilLine, Plus, Search, Send, Settings, UserRound, X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { webClient, type WsEvent } from '../../services/webClient';
import { attachTeamSession } from '../../services/teamAttach';
import ModelPicker from '../../components/ModelPicker';
import { TeamMemberAvatar } from '../../components/TeamMemberAvatar';
import { useSessionStore } from '../../stores/sessionStore';
import { useWorkspaceStore } from '../../stores/workspaceStore';
import type { ModelSelection, UserAnswer } from '../../types';
import { MarkdownRenderer } from '../../components/MarkdownRenderer';
import { Dialog } from '../../components/ui/Dialog/Dialog';
import type { GroupDirectory } from './useGroupDirectory';
import { FileDownloadList } from '../../components/ChatPanel/MessageItem';
import { getFileIdentityKey } from '../../utils/fileDownloadDedup';
import { groupChatClient, type GroupExecution, type GroupInfo, type GroupMessage, type GroupQuestion } from './client';
import { MemberPicker } from './MemberPicker';
import { ExecutionCard } from './ExecutionCard';
import { GroupFilePreviewPanel, GroupFileRow, groupFileTarget, type GroupFile } from './GroupFileRow';
import { InteractionPrompt } from '../../components/InteractionSlot/InteractionPrompt';
import '../../components/InteractionSlot/InteractionSlot.css';
import { executionResult, groupMessageContent, updateAttachments, isBackgroundRefreshTimeout, mergeExecutions, mergeMessages, selectedMentions } from './state';
import './groupChat.css';

function Attachment({ file, onPreview }: { file: GroupFile; onPreview: (file: GroupFile) => void }) {
  const { t } = useTranslation();
  const { path, name, url } = groupFileTarget(file, t('groupChat.attachment'));
  return <div className="group-chat-file" data-testid="group-chat-file">
    <FileDownloadList files={[{
      name, path, download_url: url ?? '',
      size: typeof file.size === 'number' ? file.size : 0,
      mime_type: typeof file.mime_type === 'string' ? file.mime_type : '',
      download_token: typeof file.download_token === 'string' ? file.download_token : '',
    }]} onPreview={() => onPreview(file)} />
  </div>;
}

const FINISHED_EXECUTIONS = new Set(['completed', 'failed', 'cancelled', 'interrupted', 'timed_out', 'stopped']);

/** One open group conversation with its member and file panels; the group list lives in the chat sidebar. */
export function GroupChatWorkspace({ groupId, directory, onBack }: {
  groupId: string; directory: GroupDirectory; onBack?: () => void;
}) {
  const { t } = useTranslation();
  const [group, setGroup] = useState<GroupInfo | null>(null);
  const [messages, setMessages] = useState<GroupMessage[]>([]);
  const [executions, setExecutions] = useState<GroupExecution[]>([]);
  const [questions, setQuestions] = useState<GroupQuestion[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [refreshError, setRefreshError] = useState('');
  const [addingMembers, setAddingMembers] = useState(false);
  const [panel, setPanel] = useState<'members' | 'files' | null>('members');
  const [preview, setPreview] = useState<GroupFile | null>(null);
  const [memberSearch, setMemberSearch] = useState(false);
  const [memberQuery, setMemberQuery] = useState('');
  const [input, setInput] = useState('');
  const [mentions, setMentions] = useState<string[]>([]);
  const [mentionMenu, setMentionMenu] = useState(false);
  const [mentionIndex, setMentionIndex] = useState(0);
  const mentionOptions = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (mentionMenu) {
      mentionOptions.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
    }
  }, [mentionMenu, mentionIndex]);
  const [sending, setSending] = useState(false);
  const defaultModel = useSessionStore((state) => state.defaultModelName);
  const [modelName, setModelName] = useState<string | null>(null);
  const [modelSelection, setModelSelection] = useState<ModelSelection | null>(null);
  const [fileAction, setFileAction] = useState<{ kind: 'rename' | 'delete'; path: string; name: string } | null>(null);
  const [fileName, setFileName] = useState('');
  const [fileBusy, setFileBusy] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [olderOffset, setOlderOffset] = useState(0);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [connected, setConnected] = useState(webClient.getState() === 'ready');
  const request = useRef<{ signature: string; token: string } | null>(null);
  const alive = useRef(true);
  const refreshing = useRef(false);
  const historyStart = useRef<number | null>(null);
  const historyTotal = useRef<number | null>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const answeredQuestions = useRef(new Set<string>());

  const report = useCallback((reason: unknown) => {
    if (alive.current) setError(reason instanceof Error ? reason.message : String(reason));
  }, []);

  const refresh = useCallback(async () => {
    if (!groupId || refreshing.current) return;
    refreshing.current = true;
    try {
      const info = await groupChatClient.get(groupId);
      const head = await groupChatClient.history(groupId, { limit: 1 });
      const offset = historyTotal.current === null ? Math.max(0, head.total - 50) : Math.max(0, historyTotal.current - 1);
      const history: GroupMessage[] = [];
      let messageOffset = offset;
      while (messageOffset < head.total) {
        const page = await groupChatClient.history(groupId, { limit: 200, offset: messageOffset });
        history.push(...page.messages);
        messageOffset += page.messages.length;
        if (!page.messages.length) break;
      }
      let executionOffset = 0;
      const snapshots: GroupExecution[] = [];
      while (true) {
        const page = await groupChatClient.executions(groupId, { limit: 200, offset: executionOffset });
        snapshots.push(...page.executions);
        executionOffset += page.executions.length;
        if (!page.executions.length || executionOffset >= page.total) break;
      }
      if (!alive.current) return;
      historyTotal.current = head.total;
      setRefreshError('');
      setGroup(info);
      setQuestions((info.pending_questions ?? []).filter((item) => !answeredQuestions.current.has(item.request_id)));
      setMessages((current) => mergeMessages(current, history));
      setExecutions((current) => mergeExecutions(current, snapshots));
      if (historyStart.current === null) {
        historyStart.current = offset;
        setOlderOffset(offset);
      }
    } catch (reason) {
      const backgroundTimeout = isBackgroundRefreshTimeout(reason, historyTotal.current !== null);
      if (!backgroundTimeout && alive.current) {
        setRefreshError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      refreshing.current = false;
      if (alive.current) setLoading(false);
    }
  }, [groupId, report]);

  useEffect(() => {
    alive.current = true;
    void refresh();
    void attachTeamSession(groupId, 'team').catch(report);
    const onEvent = (event: WsEvent) => {
      const payload = event.payload;
      if ((payload.group_id ?? payload.session_id) !== groupId || !groupId) return;
      const type = payload.event_type ?? event.event;
      if (type === 'team.runtime_ready') {
        void refresh();
        return;
      }
      if (type === 'chat.ask_user_question' && payload.source === 'ask_user_interrupt') {
        const pending = payload as unknown as GroupQuestion;
        if (answeredQuestions.current.has(pending.request_id)) return;
        setQuestions((current) => [...current.filter((item) => item.request_id !== pending.request_id), pending]);
        return;
      }
      if (type === 'chat.ask_user_answer') {
        if (typeof payload.request_id === 'string') answeredQuestions.current.add(payload.request_id);
        setQuestions((current) => current.filter((item) => item.request_id !== payload.request_id));
        return;
      }
      if (['chat.error', 'team.error', 'team.interact.failed'].includes(String(type))) {
        report(payload.error || payload.message || t('groupChat.sendFailed'));
        return;
      }
      if (type === 'team.group_message.accepted') {
        const message = payload.message as GroupMessage | undefined;
        if (message?.message_id) setMessages((current) => mergeMessages(current, [message]));
        return;
      }
      if (payload.event_type === 'group.message' || event.event === 'group.message') {
        const message = payload.message as GroupMessage | undefined;
        if (message?.message_id) setMessages((current) => mergeMessages(current, [{
          ...message,
          execution_id: typeof payload.execution_id === 'string' ? payload.execution_id : null,
        }]));
      } else if (String(payload.event_type ?? event.event).startsWith('group.execution.')) {
        if (typeof payload.execution_id === 'string' && typeof payload.revision === 'number') {
          setExecutions((current) => mergeExecutions(current, [payload as unknown as GroupExecution]));
        }
      } else if (payload.event_type === 'group.member.added' || event.event === 'group.member.added') {
        void refresh();
      }
    };
    const stops = ['group.message', 'group.execution.created', 'group.execution.updated',
      'chat.ask_user_question', 'chat.ask_user_answer', 'group.member.added', 'team.runtime_ready', 'team.group_message.accepted', 'chat.final', 'chat.error', 'team.error', 'team.interact.failed'].map((name) => webClient.on(name, onEvent));
    const stopState = webClient.onStateChange((state) => {
      setConnected(state === 'ready');
      if (state !== 'ready') return;
      void attachTeamSession(groupId, 'team').catch(report);
      void refresh();
    });
    const timer = window.setInterval(() => {
      if (webClient.getState() === 'ready' && document.visibilityState === 'visible') void refresh();
    }, 10_000);
    return () => {
      alive.current = false;
      stops.forEach((stop) => stop());
      stopState();
      window.clearInterval(timer);
    };
  }, [groupId, refresh, report, t]);

  useEffect(() => {
    if (directory.lastUpdated?.group_id === groupId) setGroup(directory.lastUpdated);
  }, [directory.lastUpdated, groupId]);

  useEffect(() => {
    if (atBottom.current) scroll.current?.scrollTo({ top: scroll.current.scrollHeight });
  }, [messages, executions, loading]);

  async function answerQuestion(requestId: string, answers: UserAnswer[]) {
    if (!groupId) return false;
    try {
      await groupChatClient.answer(groupId, requestId, answers);
      answeredQuestions.current.add(requestId);
      if (alive.current) setQuestions((current) => current.filter((item) => item.request_id !== requestId));
      return true;
    } catch (reason) { report(reason); return false; }
  }

  async function loadOlder() {
    if (!groupId || loadingOlder) return;
    setLoadingOlder(true);
    const offset = Math.max(0, olderOffset - 50);
    const before = scroll.current?.scrollHeight ?? 0;
    try {
      const page = await groupChatClient.history(groupId, { limit: olderOffset - offset, offset });
      if (!alive.current) return;
      atBottom.current = false;
      setMessages((current) => mergeMessages(current, page.messages));
      historyStart.current = offset;
      setOlderOffset(offset);
      requestAnimationFrame(() => {
        if (scroll.current) scroll.current.scrollTop += scroll.current.scrollHeight - before;
      });
    } catch (reason) { report(reason); }
    finally { if (alive.current) setLoadingOlder(false); }
  }

  async function send() {
    if (!group || !input.trim() || sending || !connected) return;
    const recipients = selectedMentions(input, mentions, group.members);
    const body = {
      query: input.trim(), mentions: recipients, intent: 'chat' as const,
      ...(modelName ? { model_name: modelName } : {}),
      ...(modelSelection ? { model_selection: modelSelection } : {}),
    };
    const signature = JSON.stringify(body);
    if (request.current?.signature !== signature) request.current = { signature, token: crypto.randomUUID() };
    const token = request.current.token;
    const draft = input;
    const draftMentions = mentions;
    const pendingId = `pending-${token}`;
    atBottom.current = true;
    setMessages((current) => mergeMessages(current, [{
      message_id: pendingId, client_message_id: token, sender: 'user', sender_name: '',
      content: body.query, timestamp: Date.now(), mentions: recipients, attachments: [], pending: true,
    }]));
    setInput('');
    setMentions([]);
    setSending(true);
    setError('');
    try {
      await groupChatClient.send(group.group_id, { ...body, client_message_id: token });
      if (!alive.current) return;
      request.current = null;
      atBottom.current = true;
      await refresh();
    } catch (reason) {
      setMessages((current) => current.filter((message) => message.message_id !== pendingId));
      setInput((current) => current || draft);
      setMentions((current) => current.length ? current : draftMentions);
      report(reason);
    }
    finally { if (alive.current) setSending(false); }
  }

  function mention(memberName: string) {
    const member = group?.members.find((item) => item.member_name === memberName);
    if (!member) return;
    setInput((value) => `${value.replace(/@$/, '')}@${member.display_name || member.member_name} `);
    setMentions((value) => [...new Set([...value, member.member_name])]);
    setMentionMenu(false);
    textarea.current?.focus();
  }

  function quoteFile(reference: string) {
    setInput((value) => `${value}${value && !/\s$/.test(value) ? ' ' : ''}${reference} `);
    requestAnimationFrame(() => textarea.current?.focus());
  }

  function openFileAction(kind: 'rename' | 'delete', path: string, currentName: string) {
    setError(''); setFileName(currentName); setFileAction({ kind, path, name: currentName });
  }

  async function submitFileAction() {
    if (!groupId || !fileAction || fileBusy) return;
    const nextName = fileAction.kind === 'rename' ? fileName.trim() : null;
    if (fileAction.kind === 'rename' && (!nextName || nextName === fileAction.name)) {
      setFileAction(null);
      return;
    }
    setFileBusy(true);
    try {
      if (nextName === null) await groupChatClient.deleteFile(groupId, fileAction.path);
      else await groupChatClient.renameFile(groupId, fileAction.path, nextName);
      if (!alive.current) return;
      setMessages((current) => updateAttachments(current, fileAction.path, nextName));
      setExecutions((current) => updateAttachments(current, fileAction.path, nextName));
      setPreview((current) => current?.path !== fileAction.path ? current
        : nextName === null ? null : { ...current, name: nextName });
      setFileAction(null);
    } catch (reason) { report(reason); }
    finally { if (alive.current) setFileBusy(false); }
  }

  async function clearHistory() {
    if (!groupId || clearing) return;
    setClearing(true);
    try {
      await groupChatClient.clearHistory(groupId);
      if (!alive.current) return;
      // Restart pagination from the (now shorter) server-side history.
      historyTotal.current = null;
      historyStart.current = null;
      setOlderOffset(0);
      setMessages([]);
      setExecutions((current) => current.filter((item) => !FINISHED_EXECUTIONS.has(item.status)));
      setPreview(null);
      setClearOpen(false);
      await refresh();
    } catch (reason) { report(reason); }
    finally { if (alive.current) setClearing(false); }
  }

  const files = new Map<string, Record<string, unknown>>();
  for (const message of messages) {
    for (const file of message.attachments ?? []) files.set(getFileIdentityKey(file), file);
  }
  for (const execution of executions) {
    for (const file of execution.attachments ?? []) files.set(getFileIdentityKey(file), file);
  }
  const timeline = [
    ...messages.map((message) => ({ id: message.message_id, at: message.timestamp, message, execution: null })),
    ...executions.map((execution) => ({ id: execution.execution_id, at: execution.created_at * 1000, message: null, execution })),
  ].sort((a, b) => a.at - b.at || a.id.localeCompare(b.id));

  const visibleMembers = group?.members.filter((member) => `${member.display_name ?? ''} ${member.member_name}`
    .toLocaleLowerCase().includes(memberQuery.trim().toLocaleLowerCase())) ?? [];
  const showSelf = !memberQuery.trim() || t('groupChat.you').includes(memberQuery.trim());
  const memberAvatar = (member: string) => <TeamMemberAvatar member={member} className="group-chat-assignee-avatar" alt="" />;
  const agentCount = group?.members.length ?? 0;

  return <section className="group-chat-workspace has-conversation" data-testid="group-chat-workspace">
    <div className="group-chat-main" data-testid="group-chat-main">
    <header className="group-chat-header">
      {onBack && <button type="button" className="group-chat-back" onClick={onBack} aria-label={t('groupChat.back')}
        data-testid="group-chat-back"><ChevronLeft size={19} /></button>}
      <h1 data-testid="group-chat-title">{group?.name || t('groupChat.open')}</h1>
      {group && <span className="group-chat-header-counts" data-testid="group-chat-member-count">
        <span title={t('groupChat.humanCount', { count: 1 })} aria-label={t('groupChat.humanCount', { count: 1 })}
          data-testid="group-chat-human-count"><UserRound size={12} aria-hidden />1</span>
        <span title={t('groupChat.agentCount', { count: agentCount })} aria-label={t('groupChat.agentCount', { count: agentCount })}
          data-testid="group-chat-agent-count"><Bot size={12} aria-hidden />{agentCount}</span>
      </span>}
      <div className="group-chat-header-actions">
        {group ? <>
          <button type="button" className="group-chat-icon-button" aria-label={t('groupChat.files')} aria-pressed={!preview && panel === 'files'}
            title={t('groupChat.files')} onClick={() => { setPreview(null); setPanel(panel === 'files' && !preview ? null : 'files'); }}
            data-testid="group-chat-toggle-files"><Folder size={18} /></button>
          <button type="button" className="group-chat-icon-button" aria-label={t('groupChat.settings')} aria-pressed={!preview && panel === 'members'}
            title={t('groupChat.settings')} onClick={() => { setPreview(null); setPanel(panel === 'members' && !preview ? null : 'members'); }}
            data-testid="group-chat-toggle-members"><Settings size={18} /></button>
        </> : null}
      </div>
    </header>
    {(error || refreshError) && <div className="group-chat-error" role="alert" data-testid="group-chat-error">
      <span>{error || refreshError}</span>
      <button type="button" onClick={() => {
        setError(''); setRefreshError(''); void refresh();
      }} data-testid="group-chat-retry">{t('groupChat.retry')}</button>
    </div>}
    {!connected && <p className="group-chat-notice" role="status" data-testid="group-chat-offline">{t('groupChat.offline')}</p>}
    {loading ? <div className="group-chat-empty" data-testid="group-chat-loading">{t('groupChat.loading')}</div> : group ? <div className="group-chat-body">
      <div className="group-chat-conversation">
        <div className="group-chat-messages" ref={scroll} data-testid="group-chat-messages" onScroll={() => {
          const node = scroll.current;
          if (node) atBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
        }}>
          {olderOffset > 0 && <button type="button" className="group-chat-text-button" disabled={loadingOlder}
            onClick={() => void loadOlder()} data-testid="group-chat-load-older">{t('groupChat.older')}</button>}
          {!timeline.length && <div className="group-chat-empty group-chat-start" data-testid="group-chat-empty">
            <span className="group-chat-avatar-stack" data-testid="group-chat-start-avatars">
              {(group.members.length ? group.members.slice(0, 4).map((member) => member.member_name) : ['user'])
                .map((member) => <TeamMemberAvatar key={member} member={member} className="group-chat-stack-avatar" alt="" />)}
            </span>
            <h2>{t('groupChat.start')}</h2><p>{t('groupChat.startHint')}</p>
          </div>}
          {timeline.map((item) => item.execution ? <ExecutionCard key={item.id} execution={item.execution}
            result={executionResult(item.execution, messages)} renderAvatar={memberAvatar} /> : item.message && (
            <article key={item.id} id={`group-chat-message-${item.id}`} className={`group-chat-message${item.message.sender === 'user' ? ' is-user' : ''}`}
              data-testid="group-chat-message" data-variant={item.id}>
              {item.message.sender !== 'user' && <header data-testid="group-chat-message-header">
                <TeamMemberAvatar member={item.message.sender} className="group-chat-message-avatar" alt="" />
                <strong>{item.message.sender_name || item.message.sender}</strong>
                <time dateTime={new Date(item.at).toISOString()}>{new Date(item.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time>
              </header>}
              <MarkdownRenderer content={groupMessageContent(item.message)} />
              {item.message.pending && <small className="group-chat-muted" data-testid="group-chat-message-pending">
                {t('groupChat.sending')}
              </small>}
              {(item.message.attachments ?? []).map((file, index) => <Attachment key={index} file={file} onPreview={setPreview} />)}
            </article>
          ))}
        </div>
        <div className="group-chat-composer-area">
          {questions[0] && <div className="interaction-slot" data-testid="group-chat-question">
            <p className="group-chat-question-owner" data-testid="group-chat-question-owner">
              {t('groupChat.questionFrom', { name: group.members.find(
                (member) => member.member_name === questions[0].member_name,
              )?.display_name || questions[0].member_name || group.name })}
            </p>
            <InteractionPrompt key={questions[0].request_id} pending={questions[0]} echoAnswer={false} variant="composer"
              onSubmit={answerQuestion} />
          </div>}
          {!questions[0] && <form className="group-chat-composer" data-testid="group-chat-composer" onSubmit={(event) => { event.preventDefault(); void send(); }}>
            <textarea ref={textarea} value={input} maxLength={20000}
              aria-label={t('groupChat.message')} placeholder={t('groupChat.placeholder')} data-testid="group-chat-input"
              aria-controls={mentionMenu ? 'group-chat-mention-list' : undefined}
              aria-activedescendant={mentionMenu ? `group-chat-mention-${mentionIndex}` : undefined}
              onChange={(event) => {
                setInput(event.target.value);
                setMentionIndex(0);
                setMentionMenu(event.target.value.endsWith('@'));
              }}
              onKeyDown={(event) => {
                if (event.nativeEvent.isComposing) return;
                if (mentionMenu) {
                  if (event.key === 'Escape') {
                    event.preventDefault(); setMentionMenu(false); return;
                  }
                  if (group.members.length && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
                    event.preventDefault();
                    const direction = event.key === 'ArrowDown' ? 1 : -1;
                    setMentionIndex((index) => (index + direction + group.members.length) % group.members.length);
                    return;
                  }
                  if (event.key === 'Enter' && !event.shiftKey && group.members[mentionIndex]) {
                    event.preventDefault(); mention(group.members[mentionIndex].member_name); return;
                  }
                }
                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && !mentionMenu) {
                  event.preventDefault(); void send();
                }
              }} />
            {mentionMenu && <div ref={mentionOptions} id="group-chat-mention-list" role="listbox"
              aria-label={t('groupChat.mention')} className="group-chat-mention-menu" data-testid="group-chat-mention-menu">
              {!group.members.length && <p>{t('groupChat.noMembers')}</p>}
              {group.members.map((member, index) => <button type="button" key={member.member_name}
                id={`group-chat-mention-${index}`} role="option" aria-selected={index === mentionIndex}
                onMouseEnter={() => setMentionIndex(index)}
                disabled={sending} onClick={() => mention(member.member_name)}
                data-testid="group-chat-mention-option" data-variant={member.member_name}>
                {memberAvatar(member.member_name)}<span>{member.display_name || member.member_name}</span>
                {member.kind === 'agent_group' && <small className="group-chat-tag is-team">{t('groupChat.team')}</small>}
              </button>)}
            </div>}
            <footer>
              <button type="button" className="group-chat-icon-button" disabled={sending} onClick={() => {
                setMentionIndex(0); setMentionMenu(!mentionMenu); textarea.current?.focus();
              }}
                aria-label={t('groupChat.mention')} title={t('groupChat.mention')} aria-expanded={mentionMenu}
                data-testid="group-chat-mention"><AtSign size={17} /></button>
              <span className="group-chat-muted" data-testid="group-chat-send-hint">{t('groupChat.sendHint')}</span>
              <ModelPicker value={modelName ?? defaultModel} selection={modelSelection} includeModelGroups={false}
                disabled={sending} onChange={setModelName} onSelectionChange={setModelSelection}
                testIdPrefix="group-chat-model" />
              <button type="submit" className="group-chat-send" disabled={!input.trim() || sending || !connected}
                aria-label={t('groupChat.send')} data-testid="group-chat-send"><Send size={16} /></button>
            </footer>
          </form>}
          <p className="group-chat-ai-notice" data-testid="group-chat-ai-notice">{t('groupChat.aiNotice')}</p>
        </div>
      </div>
    </div> : <div className="group-chat-empty" data-testid="group-chat-unavailable">{t('groupChat.unavailable')}</div>}
    </div>
    {group && preview && <aside className="group-chat-panel is-preview" data-testid="group-chat-panel" data-variant="preview">
      <GroupFilePreviewPanel file={preview} onBack={() => setPreview(null)} />
    </aside>}
    {group && panel && !preview && <aside className="group-chat-panel" data-testid="group-chat-panel" data-variant={panel}>
      <button type="button" className="group-chat-icon-button group-chat-panel-close" aria-label={t('common.close')}
        onClick={() => setPanel(null)} data-testid="group-chat-panel-close"><X size={18} /></button>
      {panel === 'files' ? <>
        <header><h2>{t('groupChat.files')}</h2></header>
        <div className="group-chat-file-list" data-testid="group-chat-file-list">
          {!files.size && <p className="group-chat-muted" data-testid="group-chat-no-files">{t('groupChat.noFiles')}</p>}
          {olderOffset > 0 && <button type="button" className="group-chat-text-button" disabled={loadingOlder}
            onClick={() => void loadOlder()} data-testid="group-chat-files-older">{t('groupChat.olderFiles')}</button>}
          {[...files.entries()].map(([id, file]) => <GroupFileRow key={id} file={file} onPreview={setPreview} onQuote={quoteFile}
            onRename={(path, current) => openFileAction('rename', path, current)}
            onDelete={(path, current) => openFileAction('delete', path, current)} />)}
        </div>
      </> : <>
        <div className="group-chat-group-info">
          <span className="group-chat-avatar group-chat-group-avatar"><MessagesSquare size={28} aria-hidden /></span>
          <h3 data-testid="group-chat-info-name">
            <span>{group.name}</span>
            <button type="button" className="group-chat-icon-button" aria-label={t('groupChat.edit')} title={t('groupChat.edit')}
              onClick={() => directory.openEdit(group)} data-testid="group-chat-edit"><PencilLine size={14} /></button>
          </h3>
          {group.description && <p data-testid="group-chat-info-description">{group.description}</p>}
        </div>
        <div className="group-chat-panel-heading"><h3 data-testid="group-chat-members-title">{t('groupChat.members')}</h3>
          <span>
            <button type="button" className="group-chat-icon-button" onClick={() => setAddingMembers(true)}
              aria-label={t('groupChat.addMembers')} title={t('groupChat.addMembers')}
              disabled={group.members.length >= 10} data-testid="group-chat-add-members"><Plus size={18} /></button>
            <button type="button" className="group-chat-icon-button" aria-label={t('groupChat.searchMembers')}
              title={t('groupChat.searchMembers')} aria-pressed={memberSearch} onClick={() => {
                setMemberSearch(!memberSearch); setMemberQuery('');
              }} data-testid="group-chat-search-members"><Search size={16} /></button>
          </span>
        </div>
        {memberSearch && <label className="group-chat-search group-chat-member-search">
          <Search size={14} aria-hidden />
          <input autoFocus value={memberQuery} placeholder={t('groupChat.searchMembers')} aria-label={t('groupChat.searchMembers')}
            onChange={(event) => setMemberQuery(event.target.value)} data-testid="group-chat-member-search-input" />
        </label>}
        <div className="group-chat-member-list" data-testid="group-chat-member-list">
          {showSelf && <div className="group-chat-member-row" data-testid="group-chat-self">
            <TeamMemberAvatar member="user" className="group-chat-member-avatar" alt="" />
            <span>{t('groupChat.you')}</span>
          </div>}
          {visibleMembers.map((member) => <button type="button" className="group-chat-member-row" key={member.member_name}
            disabled={sending} onClick={() => mention(member.member_name)} title={t('groupChat.mention')}
            data-testid="group-chat-member" data-variant={member.member_name}>
            <TeamMemberAvatar member={member.member_name} className="group-chat-member-avatar" alt="" />
            <span>{member.display_name || member.member_name}</span>
            {member.kind === 'agent_group' && <small className="group-chat-tag is-team">{t('groupChat.team')}</small>}
          </button>)}
          {!showSelf && !visibleMembers.length && <p className="group-chat-muted" data-testid="group-chat-no-member-match">
            {t('groupChat.noMemberMatch')}</p>}
        </div>
        <footer className="group-chat-panel-footer">
          <button type="button" className="group-chat-outline-button" disabled={clearing}
            onClick={() => { setError(''); setClearOpen(true); }} data-testid="group-chat-clear-history">
            {t('groupChat.clearHistory')}</button>
          <button type="button" className="group-chat-delete" onClick={() => directory.openDelete(group)}
            data-testid="group-chat-delete">{t('groupChat.deleteConversation')}</button>
        </footer>
      </>}
    </aside>}
    {addingMembers && group && <MemberPicker group={group} onClose={() => setAddingMembers(false)} onSaved={(saved) => {
      setAddingMembers(false);
      void useWorkspaceStore.getState().refreshWorkspaceData();
      setGroup(saved);
      void directory.reload();
    }} />}
    <Dialog open={fileAction?.kind === 'rename'} titleId="group-chat-file-rename-title" className="group-chat-small-dialog"
      closeDisabled={fileBusy} onCancel={() => setFileAction(null)}>
      {error && <p role="alert" className="group-chat-error" data-testid="group-chat-file-rename-error">{error}</p>}
      <form data-testid="group-chat-file-rename-form" onSubmit={(event) => { event.preventDefault(); void submitFileAction(); }}>
        <h2 id="group-chat-file-rename-title">{t('groupChat.renameFile')}</h2>
        <label>{t('groupChat.fileName')}<input value={fileName} maxLength={255} disabled={fileBusy} autoFocus
          onChange={(event) => setFileName(event.target.value)} data-testid="group-chat-file-rename-input" /></label>
        <footer className="group-chat-dialog-footer"><button type="button" className="btn" disabled={fileBusy}
          onClick={() => setFileAction(null)} data-testid="group-chat-file-rename-cancel">{t('common.cancel')}</button>
          <button type="submit" className="btn primary" disabled={fileBusy || !fileName.trim() || /[\\/]/.test(fileName)}
            data-testid="group-chat-file-rename-save">{t('common.save')}</button></footer>
      </form>
    </Dialog>
    <Dialog open={fileAction?.kind === 'delete'} titleId="group-chat-file-delete-title" className="group-chat-small-dialog"
      closeDisabled={fileBusy} onCancel={() => setFileAction(null)}>
      <div data-testid="group-chat-file-delete-dialog"><h2 id="group-chat-file-delete-title">{t('groupChat.deleteFile')}</h2>
        {error && <p role="alert" className="group-chat-error" data-testid="group-chat-file-delete-error">{error}</p>}
        <p className="group-chat-delete-name" data-testid="group-chat-file-delete-name">{fileAction?.name}</p>
        <p data-testid="group-chat-file-delete-warning">{t('groupChat.deleteFileWarning')}</p>
        <footer className="group-chat-dialog-footer"><button type="button" className="btn" disabled={fileBusy}
          onClick={() => setFileAction(null)} data-testid="group-chat-file-delete-cancel">{t('common.cancel')}</button>
          <button type="button" className="btn group-chat-delete" disabled={fileBusy} onClick={() => void submitFileAction()}
            data-testid="group-chat-file-delete-confirm">{t('groupChat.deleteShort')}</button></footer>
      </div>
    </Dialog>
    <Dialog open={clearOpen} titleId="group-chat-clear-title" className="group-chat-small-dialog"
      closeDisabled={clearing} onCancel={() => setClearOpen(false)}>
      <div data-testid="group-chat-clear-dialog"><h2 id="group-chat-clear-title">{t('groupChat.clearHistory')}</h2>
        {error && <p role="alert" className="group-chat-error" data-testid="group-chat-clear-error">{error}</p>}
        <p data-testid="group-chat-clear-warning">{t('groupChat.clearHistoryWarning')}</p>
        <footer className="group-chat-dialog-footer"><button type="button" className="btn" disabled={clearing}
          onClick={() => setClearOpen(false)} data-testid="group-chat-clear-cancel">{t('common.cancel')}</button>
          <button type="button" className="btn group-chat-delete" disabled={clearing} onClick={() => void clearHistory()}
            data-testid="group-chat-clear-confirm">{t('groupChat.clearHistoryConfirm')}</button></footer>
      </div>
    </Dialog>
  </section>;
}
