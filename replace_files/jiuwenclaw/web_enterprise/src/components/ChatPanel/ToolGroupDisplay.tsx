/**
 * ToolGroupDisplay 组件
 *
 * 展示工具执行实体：call 可单独显示，result 仅回填显示。
 */

import {
  useState,
  useEffect,
  useRef,
  useCallback,
} from 'react';
import { useTranslation } from 'react-i18next';
import { ToolExecution } from '../../types';
import { formatToolArguments, formatToolResult } from '../../utils';
import clsx from 'clsx';

interface ToolGroupDisplayProps {
  executions: ToolExecution[];
}

/**
 * 工具详情弹窗组件
 *
 * 以弹窗形式完整展示工具调用的参数和结果
 */
interface ToolDetailModalProps {
  execution: ToolExecution;
  onClose: () => void;
}

function ToolDetailModal({ execution, onClose }: ToolDetailModalProps) {
  const { t } = useTranslation();
  const { toolCall, result, status } = execution;
  const isTimeout = status === 'timeout';
  const isError = status === 'error';
  const isSuccess = status === 'completed' && !(result && result.result && result.result.includes('success=False'));

  
  // ESC 键关闭
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* 背景遮罩 - 点击关闭 */}
      <div
        className="absolute inset-0 bg-black/60"
        onClick={onClose}
      />

      {/* 弹窗内容 */}
      <div
        className="relative w-full max-w-2xl max-h-[85vh] overflow-hidden rounded-xl animate-rise"
        style={{
          backgroundColor: 'var(--card)',
          boxShadow: 'var(--shadow-xl)',
        }}
      >
        {/* 标题栏 */}
        <div
          className="px-6 py-4 flex items-center justify-between"
          style={{
            backgroundColor: 'var(--panel-strong)',
            borderBottom: '1px solid var(--border)',
          }}
        >
          <div className="flex items-center gap-4">
            <span className={clsx(
              'tool-pair-icon',
              isSuccess ? 'success' : isError ? 'error' : isTimeout ? 'warning' : 'pending'
            )}
            style={{ width: '32px', height: '32px' }}
            >
              {isSuccess ? (
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
              ) : isError ? (
                '❌'
              ) : isTimeout ? (
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v4m0 4h.01M12 3C7.029 3 3 7.029 3 12s4.029 9 9 9 9-4.029 9-9-4.029-9-9-9z" />
                </svg>
              ) : (
                <svg className="w-4 h-4 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
              )}
            </span>
            <div>
              <h2
                className="text-lg font-semibold font-mono"
                style={{ color: 'var(--text-strong)' }}
              >
                {toolCall.name}
              </h2>
              {toolCall.memberName && (
                <p
                  className="text-xs mt-1"
                  style={{ color: 'var(--accent, #3b82f6)' }}
                  title={toolCall.memberId || toolCall.memberName}
                >
                  @{toolCall.memberName}
                </p>
              )}
              {toolCall.formatted_args && (
                <p
                  className="text-sm font-mono mt-1"
                  style={{ color: 'var(--muted)' }}
                >
                  {toolCall.formatted_args}
                </p>
              )}
            </div>
          </div>

          {/* 关闭按钮 */}
          <button
            onClick={onClose}
            className="p-2 rounded-lg transition-colors"
            style={{ color: 'var(--muted)' }}
            onMouseEnter={(e) => {
              e.currentTarget.style.backgroundColor = 'var(--bg-hover)';
              e.currentTarget.style.color = 'var(--text)';
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.backgroundColor = 'transparent';
              e.currentTarget.style.color = 'var(--muted)';
            }}
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* 内容区域 */}
        <div
          className="px-6 py-5 overflow-y-auto"
          style={{ maxHeight: '60vh' }}
        >
          {/* 工具参数 */}
          {Object.keys(toolCall.arguments).length > 0 && (
            <div className="mb-6">
              <div
                className="flex items-center gap-2 mb-3"
                style={{ color: 'var(--text-strong)' }}
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
                </svg>
                <span className="text-sm font-semibold">{t('chatUi.toolResult.arguments')}</span>
              </div>
              <pre
                className="p-4 rounded-lg overflow-auto whitespace-pre-wrap break-all"
                style={{
                  fontFamily: 'var(--mono)',
                  fontSize: 'var(--font-size-sm)',
                  lineHeight: '1.5',
                  backgroundColor: 'var(--bg-elevated)',
                  border: '1px solid var(--border)',
                  color: 'var(--text)',
                  wordBreak: 'break-word',
                }}
              >
                {formatToolArguments(toolCall.arguments)}
              </pre>
            </div>
          )}

          {/* 工具结果 */}
          {result && (
            <div>
              <div
                className="flex items-center gap-2 mb-3"
                style={{ color: result.success && !(result.result && result.result.includes('success=False')) ? 'var(--ok)' : 'var(--danger)' }}
              >
                {result.success && !(result.result && result.result.includes('success=False')) ? (
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                ) : (
                  '❌'
                )}
                <span className="text-sm font-semibold">
                  {t('chatUi.toolResult.result')}
                  {(!result.success || (result.result && result.result.includes('success=False'))) && (
                    <span
                      className="ml-2 px-2 py-0.5 rounded text-xs font-medium"
                      style={{
                        backgroundColor: 'var(--danger-subtle)',
                        color: 'var(--danger)',
                      }}
                    >
                      {t('chatUi.toolResult.failed')}
                    </span>
                  )}
                </span>
              </div>
              <pre
                className="p-4 rounded-lg overflow-auto whitespace-pre-wrap break-all"
                style={{
                  fontFamily: 'var(--mono)',
                  fontSize: 'var(--font-size-sm)',
                  lineHeight: '1.5',
                  backgroundColor: 'var(--bg-elevated)',
                  border: '1px solid var(--border)',
                  color: result.success && !(result.result && result.result.includes('success=False')) ? 'var(--text)' : 'var(--danger)',
                  wordBreak: 'break-word',
                }}
              >
                {formatToolResult(result.result)}
              </pre>
            </div>
          )}

          {/* 超时状态 */}
          {!result && isTimeout && (
            <div
              className="flex items-center gap-3 p-4 rounded-lg"
              style={{
                backgroundColor: 'var(--warn-subtle)',
                border: '1px solid var(--warn)',
                color: 'var(--warn)',
              }}
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              <span className="font-medium">{t('chatUi.toolResult.timeout')}</span>
            </div>
          )}

          {/* 等待状态 */}
          {!result && !isTimeout && (
            <div
              className="flex items-center gap-3 p-4 rounded-lg"
              style={{
                backgroundColor: 'var(--accent-subtle)',
                border: '1px solid var(--accent)',
                color: 'var(--accent)',
              }}
            >
              <svg className="w-5 h-5 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
              <span className="font-medium">{t('chatUi.toolResult.running')}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export function ToolExecutionItem({ execution }: { execution: ToolExecution }) {
  const { t } = useTranslation();
  const [showModal, setShowModal] = useState(false);
  const { toolCall, result, status } = execution;
  const subtitle = toolCall.formatted_args || '';
  const hasResult = !!result;
  const isTimeout = status === 'timeout';
  const isError = status === 'error';
  const isSuccess = status === 'completed' && !(result && result.result && result.result.includes('success=False'));
  const resultSummary = result
    ? (result.summary || (isSuccess ? t('chatUi.toolResult.success') : '❌'))
    : '';

  return (
    <>
      <div
        className="tool-pair-item animate-rise"
        data-testid={`tool-execution-${toolCall.id}`}
        data-tool-name={toolCall.name}
        data-tool-status={status}
      >
        <div className="tool-pair-header" onClick={() => setShowModal(true)}>
          <span className={clsx(
            'tool-pair-icon',
            isSuccess ? 'success' : isError ? 'error' : isTimeout ? 'warning' : 'pending'
          )}>
            {isSuccess ? (
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
              </svg>
            ) : isError ? (
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            ) : isTimeout ? (
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v4m0 4h.01M12 3C7.029 3 3 7.029 3 12s4.029 9 9 9 9-4.029 9-9-4.029-9-9-9z" />
              </svg>
            ) : (
              <svg className="w-3 h-3 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
            )}
          </span>

          {toolCall.memberName && (
            <span
              className="tool-pair-member"
              title={toolCall.memberId || toolCall.memberName}
              style={{
                fontSize: 'var(--font-size-xs, 12px)',
                padding: '2px 6px',
                borderRadius: '4px',
                backgroundColor: 'var(--accent-subtle, rgba(59,130,246,0.12))',
                color: 'var(--accent, #3b82f6)',
                marginRight: '6px',
                whiteSpace: 'nowrap',
              }}
            >
              @{toolCall.memberName}
            </span>
          )}
          {toolCall.name === 'session' ? (
            <span className="tool-pair-name">{subtitle || t('chatUi.toolGroup.sessionCompleted')}</span>
          ) : (
            <>
              <span className="tool-pair-name">{toolCall.name}</span>
              {subtitle && <span className="tool-pair-summary">{subtitle}</span>}
            </>
          )}

          {hasResult && (
            <span className={clsx(
              'tool-pair-result-badge',
              result.success ? 'success' : 'error'
            )}>
              {resultSummary}
            </span>
          )}
          {!hasResult && isTimeout && (
            <span className="tool-pair-result-badge warning">
              {t('chatUi.toolResult.timeout')}
            </span>
          )}
          <span className="tool-pair-toggle">▶</span>
        </div>
      </div>

      {/* 弹窗 */}
      {showModal && (
        <ToolDetailModal execution={execution} onClose={() => setShowModal(false)} />
      )}
    </>
  );
}

export function ToolGroupDisplay({ executions }: ToolGroupDisplayProps) {
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLDivElement>(null);
  const [userScrolled, setUserScrolled] = useState(false);
  const totalPairs = executions.length;
  const pendingCount = executions.filter((e) => e.status === 'pending').length;
  const timeoutCount = executions.filter((e) => e.status === 'timeout').length;
  const allSessionType = totalPairs > 0 && executions.every((e) => e.toolCall.name === 'session');

  useEffect(() => {
    if (!import.meta.env.DEV) {
      return;
    }
    if (totalPairs > 0) {
      console.debug('[ws][metrics] pendingToolPairs', {
        pendingToolPairs: pendingCount,
        timeoutToolPairs: timeoutCount,
        totalToolPairs: totalPairs,
      });
    }
  }, [pendingCount, timeoutCount, totalPairs]);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    setUserScrolled(!atBottom);
  }, []);

  const scrollInner = useCallback((smooth = true) => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({
      top: el.scrollHeight,
      behavior: smooth ? 'smooth' : 'instant',
    });
  }, []);

  useEffect(() => {
    if (!userScrolled) {
      // 自动跟随新增工具项时使用即时滚动，避免出现从顶部滑下的视觉效果
      scrollInner(false);
    }
  }, [executions.length, userScrolled, scrollInner]);

  const scrollToBottom = useCallback(() => {
    setUserScrolled(false);
    scrollInner(true);
  }, [scrollInner]);

  return (
    <div className="tool-group-container animate-rise" data-testid="tool-group">
      <div className="tool-group-header">
        <div className="tool-group-header-left">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M11.42 15.17L17.25 21A2.652 2.652 0 0021 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 11-3.586-3.586l6.837-5.63m5.108-.233c.55-.164 1.163-.188 1.743-.14a4.5 4.5 0 004.486-6.336l-3.276 3.277a3.004 3.004 0 01-2.25-2.25l3.276-3.276a4.5 4.5 0 00-6.336 4.486c.091 1.076-.071 2.264-.904 2.95l-.102.085m-1.745 1.437L5.909 7.5H4.5L2.25 3.75l1.5-1.5L7.5 4.5v1.409l4.26 4.26m-1.745 1.437l1.745-1.437m6.615 8.206L15.75 15.75M4.867 19.125h.008v.008h-.008v-.008z" />
          </svg>
          <span>
            {allSessionType
              ? t('chatUi.toolGroup.sessionExecuted', { count: totalPairs })
              : t('chatUi.toolGroup.executed', { totalPairs })}
            {pendingCount > 0 && <span className="tool-group-pending"> ({t('chatUi.toolGroup.pending', { pendingCount })})</span>}
            {timeoutCount > 0 && <span className="tool-group-pending warning"> ({t('chatUi.toolGroup.timeout', { timeoutCount })})</span>}
          </span>
        </div>
      </div>

      <div ref={scrollRef} className="tool-group-scroll" onScroll={handleScroll}>
        {executions.map((execution) => (
          <ToolExecutionItem key={execution.toolCallId} execution={execution} />
        ))}
      </div>

      {userScrolled && (
        <button className="tool-group-scroll-btn" onClick={scrollToBottom}>
          {t('chatUi.toolGroup.latest')}
        </button>
      )}
    </div>
  );
}
