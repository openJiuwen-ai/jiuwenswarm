// ChatPanel/InputArea 的未发送附件草稿按 session_id 存在这个模块级 Map 里（组件卸载/
// 切换会话都不丢）。'new'（NEW_CONVERSATION_ID）是所有"还没发送过第一条消息的新对话"共用
// 的同一个占位 key：是否应该把上一次留在这个 key 下的草稿继续带给这一次的新对话，和是否
// 应该继续带上次的输入框文字、专家选择是同一个判断——是不是真的在"回到刚才那个还没发出去
// 的新对话"，而不是"另开一个全新的、不相关的新对话"。那个判断已经在 multi-session 生命周期
// 那边（App.tsx 的 enterNewConversation / pendingNewConversationRef）做了，所以这里只负责
// 存储本身和按 session_id 清空；放在独立模块里是为了让 InputArea.tsx 和
// newConversationLifecycle.ts 都能读写同一份数据，不用互相 import 对方绕出循环依赖。
export interface AttachmentDraftLike {
  id: string;
}

export const attachmentDraftsBySession = new Map<string, AttachmentDraftLike[]>();

export function clearAttachmentDrafts(sessionId: string): void {
  attachmentDraftsBySession.delete(sessionId);
}
