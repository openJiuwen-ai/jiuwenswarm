/**
 * GoalCompletedCard — 目标完成回显卡
 *
 * 由 MessageItem 检测到 `goal.completed:` 前缀消息时渲染，展示目标完成时模型自评的完成依据
 * （last_assessment.evidence）。跟普通回复气泡明显区分开，参照 QaSummaryCard 的卡片结构。
 */

import { Target } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { parseGoalCompletedContent } from './goalCompletedMessage';
import './GoalBar.css';

interface GoalCompletedCardProps {
  content: string;
}

export function GoalCompletedCard({ content }: GoalCompletedCardProps) {
  const { t } = useTranslation();
  const data = parseGoalCompletedContent(content);
  if (!data) return null;

  return (
    <div className="flex justify-start mb-3 animate-rise">
      <div className="goal-completed-card">
        <div className="goal-completed-card__head">
          <Target size={14} strokeWidth={2} className="goal-completed-card__head-icon" />
          <span>{t('goal.completedCardTitle')}</span>
        </div>
        <div className="goal-completed-card__body">
          {data.evidence?.trim() || t('goal.completedMessageFallback')}
        </div>
      </div>
    </div>
  );
}
