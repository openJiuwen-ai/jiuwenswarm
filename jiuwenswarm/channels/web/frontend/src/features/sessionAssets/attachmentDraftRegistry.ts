// ChatPanel/InputArea 的未发送附件草稿按 session_id 存在这个模块级 Map 里（组件卸载/
// 切换会话都不丢）。'new'（NEW_CONVERSATION_ID）是所有"还没发送过第一条消息的新对话"共用
// 的同一个占位 key：是否应该把上一次留在这个 key 下的草稿继续带给这一次的新对话，和是否
// 应该继续带上次的输入框文字、专家选择是同一个判断——是不是真的在"回到刚才那个还没发出去
// 的新对话"，而不是"另开一个全新的、不相关的新对话"。那个判断已经在 multi-session 生命周期
// 那边（App.tsx 的 enterNewConversation / pendingNewConversationRef）做了，所以这里只负责
// 存储本身和按 session_id 清空；放在独立模块里是为了让 InputArea.tsx 和
// newConversationLifecycle.ts 都能读写同一份数据，不用互相 import 对方绕出循环依赖。
import { create } from 'zustand';

export interface AttachmentDraftLike {
  id: string;
}

export const attachmentDraftsBySession = new Map<string, AttachmentDraftLike[]>();

interface ClearSignalState {
  /**
   * 按 session_id 计数，每次 clearAttachmentDrafts 清掉该 session 的草稿就 +1。
   * InputArea 的本地 attachments state 只在"切换会话"那个 effect 里才会从 Map 重新
   * 读取——如果 clearAttachmentDrafts 发生时 activeSessionId 根本没变（典型场景：
   * 已经停留在 'new' 欢迎页、带着未发送附件再点一次"新建对话"），那个 effect 不会跑，
   * Map 清空了但 InputArea 内存里的附件卡片仍然原样留着，之后一发送又被重新注册成
   * 这条新会话自己的素材——清空等于没生效。这个计数就是让 InputArea 即使
   * activeSessionId 没变化，也能感知到"我这个 session 的草稿被清过了"，从而主动
   * 清空本地 state。
   */
  nonce: Record<string, number>;
}

const useClearSignal = create<ClearSignalState>(() => ({ nonce: {} }));

/** InputArea 订阅当前 activeSessionId 对应的清空计数；数值变化即代表这个 session 的
 * 草稿刚被外部清掉过（不区分清了几次，只看"和上次看到的值是否不同"）。 */
export function useAttachmentDraftClearNonce(sessionId: string | null | undefined): number {
  return useClearSignal((state) => state.nonce[sessionId ?? ''] ?? 0);
}

export function clearAttachmentDrafts(sessionId: string): void {
  attachmentDraftsBySession.delete(sessionId);
  useClearSignal.setState((state) => ({
    nonce: { ...state.nonce, [sessionId]: (state.nonce[sessionId] ?? 0) + 1 },
  }));
}
