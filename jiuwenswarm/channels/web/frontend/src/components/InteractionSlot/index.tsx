/**
 * InteractionSlot — 输入框正上方的交互吸附槽
 *
 * 读取当前会话 pendingQuestion，按 source 分流：
 *  - authorization → AuthorizationPrompt（授权条）
 *  - interaction   → InteractionPrompt（交互卡）
 *  - legacy/none   → 不渲染（演进/计划审批仍由消息流内的 InlineQuestionCard 处理）
 *
 * 不参与消息滚动，紧贴输入框顶部。
 */

import { useChatStore } from '../../stores';
import type { UserAnswer } from '../../types';
import { AuthorizationPrompt } from './AuthorizationPrompt';
import { InteractionPrompt } from './InteractionPrompt';
import { classifyPrompt } from './promptRouting';
import './InteractionSlot.css';

interface InteractionSlotProps {
  onSubmit: (requestId: string, answers: UserAnswer[], source?: string) => void;
}

export function InteractionSlot({ onSubmit }: InteractionSlotProps) {
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const pending = useChatStore(
    (s) => s.runtimes[activeSessionId ?? '']?.pendingQuestion ?? null,
  );

  const kind = classifyPrompt(pending);
  if (!pending || kind === 'none' || kind === 'legacy') {
    return null;
  }

  // 授权条：页签式吸附输入框顶部；交互卡：独立浮卡。
  const isAuth = kind === 'authorization';
  return (
    <div
      className={`interaction-slot${isAuth ? ' interaction-slot--attached' : ''}`}
      data-testid="interaction-slot-root"
      data-variant={isAuth ? 'auth' : 'interaction'}
    >
      {isAuth ? (
        <AuthorizationPrompt pending={pending} onSubmit={onSubmit} />
      ) : (
        <InteractionPrompt pending={pending} onSubmit={onSubmit} />
      )}
    </div>
  );
}
