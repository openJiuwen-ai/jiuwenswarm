import { useCallback } from 'react';
import { ApplicationPluginTaskRuntimes } from './ApplicationPluginOutlet';
import { useChatStore } from '../stores/chatStore';
import { useSessionAssetsStore } from '../features/sessionAssets/sessionAssets';
import { resolveFilePath } from '../utils/fileDownloadDedup';
import { generateUuidV4 } from '../utils/uuid';

// App-owned runtimes keep media and conversation delivery alive across navigation.
export function ApplicationConversationRuntimes({ sessionId }: { sessionId: string | null }) {
  const handlePluginConversationItem = useCallback((sid: string, role: 'user' | 'assistant', text: string, presentation?: 'tool_result') => {
    const content = text.trim();
    if (!content) return;
    useChatStore.getState().addMessage(sid, {
      id: `full-duplex-${generateUuidV4()}`,
      role,
      content,
      presentation,
      keepExpanded: role === 'assistant',
      timestamp: new Date().toISOString(),
    });
  }, []);
  const handlePluginAssistantStream = useCallback(
    (sid: string, update: { streamId: string; content: string; final: boolean }) => {
      const streamId = update.streamId.trim();
      if (!streamId) return;
      const messageId = `full-duplex-${streamId}`;
      const chatStore = useChatStore.getState();
      const runtime = chatStore.getRuntime(sid);
      const existing = runtime?.messages.find((message) => message.id === messageId);
      const content = update.content.trim();

      if (!existing && !content) return;
      if (!existing) {
        chatStore.addMessage(sid, {
          id: messageId,
          role: 'assistant',
          content,
          keepExpanded: true,
          timestamp: new Date().toISOString(),
          isStreaming: !update.final,
          ...(update.final ? { completedAt: new Date().toISOString() } : {}),
        });
        if (!update.final && !runtime?.currentStreamId) {
          chatStore.startStreaming(sid, messageId, messageId);
        }
        return;
      }

      chatStore.updateMessage(sid, messageId, {
        ...(content ? { content } : {}),
        keepExpanded: true,
        isStreaming: !update.final,
        ...(update.final ? { completedAt: new Date().toISOString() } : {}),
      });
      if (update.final && chatStore.getRuntime(sid)?.currentStreamId === messageId) {
        chatStore.stopStreaming(sid, messageId);
      }
    },
    [],
  );
  const handlePluginToolCall = useCallback(
    (
      sid: string,
      toolCall: Parameters<ReturnType<typeof useChatStore.getState>['addToolCall']>[1],
      startedAt?: string,
    ) => {
      useChatStore.getState().addToolCall(sid, toolCall, { startedAt });
    },
    [],
  );
  const handlePluginToolResult = useCallback(
    (
      sid: string,
      toolResult: Parameters<ReturnType<typeof useChatStore.getState>['addToolResult']>[1],
      updatedAt?: string,
    ) => {
      useChatStore.getState().addToolResult(sid, toolResult, { updatedAt });
    },
    [],
  );
  const handlePluginReasoning = useCallback((sid: string, content: string, atMs?: number) => {
    useChatStore.getState().appendReasoning(sid, content, { atMs });
  }, []);
  const handlePluginFileItems = useCallback(
    (
      sid: string,
      files: Parameters<ReturnType<typeof useChatStore.getState>['addFileItems']>[1],
      timestampIso?: string,
    ) => {
      useChatStore.getState().ensureRuntime(sid);
      useChatStore.getState().addFileItems(sid, files, { timestampIso });
      const paths = [...new Set(files.map(resolveFilePath).filter((path): path is string => Boolean(path)))];
      void useSessionAssetsStore.getState()
        .register(sid, paths.map(path => ({ path, source: 'generated' })))
        .catch(error => console.warn('Failed to register Full-duplex session assets:', error));
    },
    [],
  );
  const handlePluginReasoningClose = useCallback((sid: string, atMs?: number) => {
    useChatStore.getState().closeReasoning(sid, { atMs });
  }, []);
  return (
      <ApplicationPluginTaskRuntimes
        sessionId={sessionId}
        onConversationItem={handlePluginConversationItem}
        onAssistantStream={handlePluginAssistantStream}
        onReasoning={handlePluginReasoning}
        onReasoningClose={handlePluginReasoningClose}
        onToolCall={handlePluginToolCall}
        onToolResult={handlePluginToolResult}
        onFileItems={handlePluginFileItems}
      />
  );
}
