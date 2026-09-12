/**
 * ProactiveRecommendationCard component
 *
 * Displays proactive recommendations with gradient styling based on type.
 * - skill_recommend: Blue-purple gradient
 * - task_reminder: Amber-orange gradient
 * - need_exploration: Green-cyan gradient
 *
 * Includes feedback buttons (like/dislike) for strategy optimization.
 */

import React, { useState, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Sparkles, Clock, Compass, ThumbsUp, ThumbsDown } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import type { Message } from '../../types';
import { formatTimestamp } from '../../utils';
import { webClient } from '../../services/webClient';

interface ProactiveRecommendationCardProps {
  message: Message;
}

// ── 反馈状态本地持久化 ──────────────────────────────────────────────
// 后端 buffer 持久化反馈，但前端无法查询"某 rec_id 是否已反馈"。刷新页面或组件
// 重建后，按钮的"已记录"态会丢失，用户会重复点击（后端去重会丢弃，但 UX 不好）。
// 用 localStorage 记 rec_id -> 'like'|'dislike'，让按钮回显已反馈态。
// key 带前缀避免撞名；纯本地 UI 态，后端仍是反馈的权威来源。
const FEEDBACK_LS_PREFIX = 'proactive-feedback:';

function loadFeedbackGiven(recId?: string): 'like' | 'dislike' | null {
  if (!recId || typeof window === 'undefined') return null;
  try {
    const v = window.localStorage.getItem(FEEDBACK_LS_PREFIX + recId);
    return v === 'like' || v === 'dislike' ? v : null;
  } catch {
    return null;
  }
}

function saveFeedbackGiven(recId: string, type: 'like' | 'dislike'): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(FEEDBACK_LS_PREFIX + recId, type);
  } catch {
    // localStorage 不可用（隐私模式等）时静默降级，不影响点赞请求本身。
  }
}

const typeConfig = {
  skill_recommend: {
    icon: Sparkles,
    gradient: 'from-indigo-500/20 via-purple-500/15 to-pink-500/20',
    border: 'border-indigo-400/40',
    iconColor: 'text-indigo-400',
    labelColor: 'text-indigo-300',
  },
  task_reminder: {
    icon: Clock,
    gradient: 'from-orange-500/20 via-amber-500/15 to-yellow-500/20',
    border: 'border-orange-400/40',
    iconColor: 'text-orange-400',
    labelColor: 'text-orange-300',
  },
  need_exploration: {
    icon: Compass,
    gradient: 'from-emerald-500/20 via-teal-500/15 to-cyan-500/20',
    border: 'border-emerald-400/40',
    iconColor: 'text-emerald-400',
    labelColor: 'text-emerald-300',
  },
};

export const ProactiveRecommendationCard: React.FC<ProactiveRecommendationCardProps> = ({ message }) => {
  const { t } = useTranslation();
  const proactiveType = message.proactiveType || 'skill_recommend';
  const config = typeConfig[proactiveType] || typeConfig.skill_recommend;
  const Icon = config.icon;
  const label = t(`config.proactive.typeLabel.${proactiveType}`, { defaultValue: t('config.proactive.typeLabel.skill_recommend') });

  const [feedbackGiven, setFeedbackGiven] = useState<'like' | 'dislike' | null>(() =>
    loadFeedbackGiven(message.proactiveRecId),
  );

  const handleFeedback = useCallback((type: 'like' | 'dislike') => {
    if (feedbackGiven) return;
    const recId = message.proactiveRecId;
    if (!recId) {
      console.warn('[ProactiveCard] no proactiveRecId, cannot send feedback');
      return;
    }

    webClient.request('proactive.feedback', {
      rec_id: recId,
      feedback_type: type === 'like' ? 'explicit_like' : 'explicit_dislike',
      // 带上推荐元数据，后端 history 未写入时兜底填充反馈记录（避免时序竞态丢反馈）。
      proactive_type: message.proactiveType,
      proactive_target: message.proactiveTarget,
    }).catch((err: unknown) => {
      console.warn('[ProactiveCard] feedback request failed:', err);
    });

    saveFeedbackGiven(recId, type);
    setFeedbackGiven(type);
  }, [feedbackGiven, message.proactiveRecId, message.proactiveType, message.proactiveTarget]);

  return (
    <div className="proactive-recommendation-card animate-fade-in" data-testid="chat-panel-proactive-recommendation-card">
      <div className={`proactive-card bg-gradient-to-br ${config.gradient} border ${config.border} rounded-lg p-4`} data-testid="chat-panel-proactive-recommendation-inner" data-variant={proactiveType}>
        {/* Header with icon and label */}
        <div className="flex items-center gap-2 mb-3" data-testid="chat-panel-proactive-recommendation-header">
          <Icon className={`w-5 h-5 ${config.iconColor}`} strokeWidth={2} data-testid="chat-panel-proactive-recommendation-icon" />
          <span className={`text-sm font-semibold ${config.labelColor}`} data-testid="chat-panel-proactive-recommendation-label">
            {label}
          </span>
        </div>

        {/* Content */}
        <div className="proactive-card-content prose prose-sm max-w-none" data-testid="chat-panel-proactive-recommendation-content">
          <ReactMarkdown>{message.content}</ReactMarkdown>
        </div>

        {/* Feedback buttons */}
        {message.proactiveRecId && (
          <div className="flex items-center gap-2 mt-3" data-testid="chat-panel-proactive-recommendation-feedback">
            <button
              onClick={() => handleFeedback('like')}
              disabled={!!feedbackGiven}
              aria-pressed={feedbackGiven === 'like'}
              data-testid="chat-panel-proactive-feedback-like"
              className={
                'flex items-center gap-1 px-2.5 py-1 rounded-full text-xs transition-colors border ' +
                (feedbackGiven === 'like'
                  ? 'bg-green-500/30 border-green-500/60 text-green-300'
                  : feedbackGiven === 'dislike'
                    ? 'bg-green-500/10 border-green-500/20 text-green-400/60 opacity-40 cursor-not-allowed'
                    : 'bg-green-500/10 hover:bg-green-500/25 text-green-400 border-green-500/20 hover:border-green-500/40')
              }
            >
              <ThumbsUp className="w-3.5 h-3.5" />
              {t('proactive.feedback.helpful', '有帮助')}
            </button>
            <button
              onClick={() => handleFeedback('dislike')}
              disabled={!!feedbackGiven}
              aria-pressed={feedbackGiven === 'dislike'}
              data-testid="chat-panel-proactive-feedback-dislike"
              className={
                'flex items-center gap-1 px-2.5 py-1 rounded-full text-xs transition-colors border ' +
                (feedbackGiven === 'dislike'
                  ? 'bg-red-500/30 border-red-500/60 text-red-300'
                  : feedbackGiven === 'like'
                    ? 'bg-red-500/10 border-red-500/20 text-red-400/60 opacity-40 cursor-not-allowed'
                    : 'bg-red-500/10 hover:bg-red-500/25 text-red-400 border-red-500/20 hover:border-red-500/40')
              }
            >
              <ThumbsDown className="w-3.5 h-3.5" />
              {t('proactive.feedback.notNeeded', '不需要')}
            </button>
          </div>
        )}

        {/* Timestamp */}
        {message.timestamp && (
          <div className="flex items-center gap-3 text-sm mt-2 text-text-muted" data-testid="chat-panel-proactive-recommendation-timestamp">
            <span>{formatTimestamp(message.timestamp)}</span>
          </div>
        )}
      </div>
    </div>
  );
};
