import type { Session } from '../../../types/index.ts';
import type { ChatRuntime } from '../../../stores/chatStore.ts';
import { normalizeAgentMode } from '../../../stores/sessionStore.ts';
import { lastPresentationReply, lastPresentationUserMessage, presentationReplyUserMessage } from './reply.ts';
import type { PresentationActivity, PresentationConversation } from './protocol.ts';

function pendingTurnTools(runtime: ChatRuntime | undefined) {
  const requestId = lastPresentationUserMessage(runtime)?.inputRequestId;
  return [...(runtime?.toolExecutions.values() ?? [])].filter((tool) => tool.status === 'pending'
    && requestId !== null && (requestId === undefined || tool.requestId === requestId));
}

export function conversationActivity(runtime: ChatRuntime | undefined): PresentationActivity {
  if (!runtime) return 'idle';
  if (runtime.pendingQuestions.length || runtime.pendingA4PAuthorization) return 'needsInput';
  if (runtime.error || runtime.executionError) return 'failed';
  if (pendingTurnTools(runtime).length) return 'toolRunning';
  if (runtime.isThinking) return 'thinking';
  if (runtime.isProcessing || runtime.currentStreamContent) return 'replying';
  if (runtime.isPaused) return 'paused';
  return lastPresentationReply(runtime) ? 'done' : 'idle';
}

export function presentationConversations(
  sessions: readonly Session[], runtimes: Readonly<Record<string, ChatRuntime>>, newTitle: string,
  identifyReply: (reply: object | null) => number | null = () => null,
): PresentationConversation[] {
  const metadata = new Map(sessions.filter((session) => normalizeAgentMode(session.mode) === 'agent')
    .map((session) => [session.session_id, session]));
  return ['new', ...metadata.keys()].map((key) => {
    const runtime = runtimes[key];
    const reply = lastPresentationReply(runtime);
    const lastUser = lastPresentationUserMessage(runtime);
    const userText = Array.from(lastUser?.content ?? '');
    const pendingTools = pendingTurnTools(runtime);
    const session = metadata.get(key);
    const lastUserMessageAt = session?.last_user_message_at;
    return {
      conversationKey: key, sessionId: key, teamId: null,
      title: session?.display_title || session?.title || newTitle,
      preview: Array.from(reply?.content ?? '').slice(0, 2000).join(''), replyId: identifyReply(reply),
      userMessageId: identifyReply(lastUser),
      inputMessageId: lastUser?.id ?? null,
      replyToMessageId: presentationReplyUserMessage(runtime, reply)?.id ?? null,
      inputOutcome: lastUser?.inputOutcome ?? null,
      latestUserMessage: userText.slice(0, 200).join('') + (userText.length > 200 ? '…' : ''),
      lastUserMessageAt: typeof lastUserMessageAt === 'number' && Number.isFinite(lastUserMessageAt)
        ? (lastUserMessageAt < 1e11 ? lastUserMessageAt * 1000 : lastUserMessageAt) : null,
      draft: runtime?.inputValue ?? '', activity: conversationActivity(runtime),
      toolNames: [...new Set(pendingTools.map((tool) => tool.toolCall.name))],
      canSend: !runtime?.isProcessing && !runtime?.pendingQuestions.length && !runtime?.pendingA4PAuthorization,
      mode: 'agent', modelId: null,
    };
  });
}
