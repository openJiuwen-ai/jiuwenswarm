/**
 * SkillPanel 全局 toast / 进度提示条
 *
 * 从 index.tsx 抽取，结构与样式保持不变。
 */
import { useTranslation } from 'react-i18next';
import type { SkillToastType } from './useSkillToasts';

interface SkillToastsProps {
  knowledgeTaskCount: number;
  message: string | null;
  messageType: SkillToastType | null;
  cleanMessage: string;
  onCloseMessage: () => void;
}

export function SkillToasts({
  knowledgeTaskCount,
  message,
  messageType,
  cleanMessage,
  onCloseMessage,
}: SkillToastsProps) {
  const { t } = useTranslation();
  return (
    <>
      {knowledgeTaskCount > 0 && (
        <div
          className="fixed top-4 right-4 z-[9999] rounded-[4px] text-sm text-text shadow-lg flex items-center gap-3 px-4 bg-card border border-border"
          style={{ width: '564px', height: '40px' }}
          data-testid="skill-panel-knowledge-progress"
        >
          <span className="w-4 h-4 border-2 border-accent border-t-transparent rounded-full animate-spin flex-shrink-0" />
          <span className="flex-1 truncate">
            {knowledgeTaskCount > 1
              ? t('skills.messages.knowledgeSkillCreatingCount', { count: knowledgeTaskCount })
              : t('skills.messages.knowledgeSkillCreating')}
          </span>
        </div>
      )}
      {message && messageType === 'success' && (
        <div
          className="fixed right-4 z-[9999] rounded-[4px] text-sm text-text shadow-lg flex items-center gap-3 px-4"
          style={{
            backgroundColor: 'var(--color-feedback-success-toast)',
            width: '564px',
            height: '40px',
            top: knowledgeTaskCount > 0 ? '4.5rem' : '1rem',
          }}
          data-testid="skill-panel-toast"
          data-variant="success"
        >
          <span className="w-4 h-4 rounded-full bg-[var(--color-feedback-success-indicator)] flex items-center justify-center flex-shrink-0">
            <svg className="w-3 h-3 text-text-inverse" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
            </svg>
          </span>
          {cleanMessage}
          <button
            type="button"
            onClick={onCloseMessage}
            className="ml-auto w-5 h-5 flex items-center justify-center hover:bg-card/30 rounded-full "
            data-testid="skill-panel-toast-close-btn"
          >
            <svg className="w-4 h-4 text-text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      )}
      {message && messageType === 'error' && (
        <div
          className="fixed right-4 z-[9999] rounded-[4px] text-sm text-text shadow-lg flex items-center gap-3 px-4 border border-danger"
          style={{
            backgroundColor: 'var(--color-feedback-danger-toast)',
            width: '564px',
            minHeight: '40px',
            top: knowledgeTaskCount > 0 ? '4.5rem' : '1rem',
          }}
          data-testid="skill-panel-toast"
          data-variant="error"
        >
          <span className="w-4 h-4 rounded-full bg-danger flex items-center justify-center flex-shrink-0 text-text-inverse text-[10px] font-bold">
            !
          </span>
          <span className="flex-1 py-2 break-words">{cleanMessage}</span>
          <button
            type="button"
            onClick={onCloseMessage}
            className="ml-auto w-5 h-5 flex items-center justify-center hover:bg-card/30 rounded-full "
            data-testid="skill-panel-toast-close-btn"
          >
            <svg className="w-4 h-4 text-text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      )}
      {message && messageType === 'loading' && knowledgeTaskCount <= 0 && (
        <div
          className="fixed top-4 right-4 z-[9999] rounded-[4px] text-sm text-text shadow-lg flex items-center gap-3 px-4 bg-card border border-border"
          style={{ width: '564px', height: '40px' }}
          data-testid="skill-panel-toast"
          data-variant="loading"
        >
          <span className="w-4 h-4 border-2 border-accent border-t-transparent rounded-full animate-spin flex-shrink-0" />
          {cleanMessage}
        </div>
      )}
    </>
  );
}
