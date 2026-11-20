import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage, upsertConversation, type JiuwenMateConversation } from './jiuwenmateApi';
import type { JiuwenMateChatApi } from './usePrivateChat';

/**
 * The listed Web conversations of one colleague, read again when the colleague changes or the
 * connection comes back. ``loaded`` stays false until the list of the current colleague arrives.
 */
export function useConversations(api: JiuwenMateChatApi, agentId: string, connectionEpoch: number) {
  const [conversations, setConversations] = useState<JiuwenMateConversation[]>([]);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const agentRef = useRef(agentId);

  useEffect(() => {
    if (agentRef.current !== agentId) {
      agentRef.current = agentId;
      setConversations([]);
      setLoadedFor(null);
      setError(null);
    }
    if (!agentId) return;
    let active = true;
    api.listConversations(agentId).then(
      (listed) => {
        if (!active) return;
        setConversations(listed);
        setLoadedFor(agentId);
      },
      (reason: unknown) => {
        if (!active) return;
        setError(errorMessage(reason));
        setLoadedFor(agentId);
      },
    );
    return () => {
      active = false;
    };
  }, [api, agentId, connectionEpoch]);

  const upsert = useCallback(
    (conversation: JiuwenMateConversation) => {
      if (agentRef.current === agentId && conversation.id) {
        setConversations((current) => upsertConversation(current, conversation));
      }
    },
    [agentId],
  );

  const rename = useCallback(
    async (conversationId: string, title: string) => {
      setError(null);
      try {
        const renamed = await api.renameConversation(agentId, conversationId, title);
        if (agentRef.current !== agentId) return;
        setConversations((current) =>
          current.map((item) => (item.id === renamed.id ? { ...renamed, lastMessage: item.lastMessage } : item)),
        );
      } catch (reason) {
        if (agentRef.current === agentId) setError(errorMessage(reason));
      }
    },
    [api, agentId],
  );

  const remove = useCallback(
    async (conversationId: string): Promise<boolean> => {
      setError(null);
      try {
        await api.deleteConversation(agentId, conversationId);
        if (agentRef.current === agentId) {
          setConversations((current) => current.filter((item) => item.id !== conversationId));
        }
        return true;
      } catch (reason) {
        if (agentRef.current === agentId) setError(errorMessage(reason));
        return false;
      }
    },
    [api, agentId],
  );

  const setPinned = useCallback(
    async (conversationId: string, pinned: boolean): Promise<JiuwenMateConversation | null> => {
      setError(null);
      try {
        const updated = await api.setConversationPinned(agentId, conversationId, pinned);
        if (agentRef.current !== agentId) return null;
        setConversations((current) =>
          current.map((item) =>
            item.id === updated.id ? { ...updated, lastMessage: item.lastMessage } : item,
          ),
        );
        return updated;
      } catch (reason) {
        if (agentRef.current === agentId) setError(errorMessage(reason));
        return null;
      }
    },
    [api, agentId],
  );

  const reload = useCallback(async () => {
    if (!agentId) return;
    try {
      const listed = await api.listConversations(agentId);
      if (agentRef.current !== agentId) return;
      setConversations(listed);
      setLoadedFor(agentId);
      setError(null);
    } catch (reason) {
      if (agentRef.current === agentId) setError(errorMessage(reason));
    }
  }, [api, agentId]);

  return { conversations, loaded: loadedFor === agentId, error, upsert, rename, remove, setPinned, reload };
}
