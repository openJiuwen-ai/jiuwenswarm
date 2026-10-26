/**
 * ProactiveRecommendationCard component
 *
 * Displays proactive recommendations with gradient styling based on type.
 * - skill_recommend: Blue-purple gradient
 * - task_reminder: Amber-orange gradient
 * - need_exploration: Green-cyan gradient
 */

import React from 'react';
import { useTranslation } from 'react-i18next';
import { Sparkles, Clock, Compass } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import type { Message } from '../../types';
import { formatTimestamp } from '../../utils';

interface ProactiveRecommendationCardProps {
  message: Message;
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

  return (
    <div className="proactive-recommendation-card animate-fade-in">
      <div className={`proactive-card bg-gradient-to-br ${config.gradient} border ${config.border} rounded-lg p-4`}>
        {/* Header with icon and label */}
        <div className="flex items-center gap-2 mb-3">
          <Icon className={`w-5 h-5 ${config.iconColor}`} strokeWidth={2} />
          <span className={`text-sm font-semibold ${config.labelColor}`}>
            {label}
          </span>
        </div>

        {/* Content */}
        <div className="proactive-card-content prose prose-sm max-w-none">
          <ReactMarkdown>{message.content}</ReactMarkdown>
        </div>

        {/* Timestamp */}
        {message.timestamp && (
          <div className="flex items-center gap-3 text-sm mt-2 text-text-muted">
            <span>{formatTimestamp(message.timestamp)}</span>
          </div>
        )}
      </div>
    </div>
  );
};
