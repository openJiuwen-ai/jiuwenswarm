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
    <div className="flex justify-start mb-3 animate-rise">
      <div className="qa-summary">
        {data.title && (
          <div className="qa-summary__head">
            <FileText size={14} strokeWidth={2} className="qa-summary__head-icon" />
            <span>{data.title}</span>
          </div>
        )}
        <div className="qa-summary__list">
          {data.items.map((item, idx) => (
            <div className="qa-summary__item" key={idx}>
              <div className="qa-summary__q">
                <span className="qa-summary__q-index">{idx + 1}.</span>
                <span>{item.question}</span>
              </div>
              <div className="qa-summary__answers">
                {item.answers.length > 0 ? (
                  item.answers.map((ans, i) => (
                    <div className="qa-summary__a" key={i}>
                      {ans}
                    </div>
                  ))
                ) : (
                  <div className="qa-summary__a qa-summary__a--empty">—</div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
