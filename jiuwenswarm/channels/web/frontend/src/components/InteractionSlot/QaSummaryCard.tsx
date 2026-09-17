/**
 * QaSummaryCard — 交互问答「问题澄清」回显卡
 *
 * 由 MessageItem 检测到 `qa.summary:` 前缀消息时渲染，展示用户对 ask_user
 * 各问题的作答结果。仅用于交互类弹窗；授权类不回显。
 */

import { FileText } from 'lucide-react';
import { parseQaSummaryContent } from './qaSummary';

interface QaSummaryCardProps {
  content: string;
}

export function QaSummaryCard({ content }: QaSummaryCardProps) {
  const data = parseQaSummaryContent(content);
  if (!data) return null;

  return (
    <div className="flex justify-start mb-3 animate-rise" data-testid="interaction-slot-qa-summary-wrap">
      <div className="qa-summary" data-testid="interaction-slot-qa-summary">
        {data.title && (
          <div className="qa-summary__head" data-testid="interaction-slot-qa-summary-head">
            <FileText size={14} strokeWidth={2} className="qa-summary__head-icon" data-testid="interaction-slot-qa-summary-head-icon" />
            <span data-testid="interaction-slot-qa-summary-head-title">{data.title}</span>
          </div>
        )}
        <div className="qa-summary__list" data-testid="interaction-slot-qa-summary-list">
          {data.items.map((item, idx) => (
            <div className="qa-summary__item" key={idx} data-testid="interaction-slot-qa-summary-item" data-variant={idx}>
              <div className="qa-summary__q" data-testid="interaction-slot-qa-summary-item-question">
                <span className="qa-summary__q-index" data-testid="interaction-slot-qa-summary-item-question-index">{idx + 1}.</span>
                <span data-testid="interaction-slot-qa-summary-item-question-text">{item.question}</span>
              </div>
              <div className="qa-summary__answers" data-testid="interaction-slot-qa-summary-item-answers">
                {item.answers.length > 0 ? (
                  item.answers.map((ans, i) => (
                    <div className="qa-summary__a" key={i} data-testid="interaction-slot-qa-summary-item-answer">
                      {ans}
                    </div>
                  ))
                ) : (
                  <div className="qa-summary__a qa-summary__a--empty" data-testid="interaction-slot-qa-summary-item-answer-empty">—</div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
