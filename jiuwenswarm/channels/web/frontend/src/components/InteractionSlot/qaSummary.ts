/**
 * 交互问答「确认后回显」的消息序列化。
 *
 * 交互类弹窗（ask_user）提交后，前端本地合成一条 assistant 消息插入对话流，
 * 内容以 `qa.summary:` 前缀 + JSON 编码。MessageItem 检测到该前缀时渲染
 * QaSummaryCard。授权类弹窗不回显，故不使用此模块。
 */

export const QA_SUMMARY_PREFIX = 'qa.summary:';

export interface QaSummaryItem {
  header?: string;
  question: string;
  /** 用户选中的选项标签 + 自定义输入（若有），已合并为展示用字符串数组 */
  answers: string[];
}

export interface QaSummaryData {
  title?: string;
  items: QaSummaryItem[];
}

export function buildQaSummaryContent(data: QaSummaryData): string {
  return QA_SUMMARY_PREFIX + JSON.stringify(data);
}

export function isQaSummaryContent(content: string | undefined | null): boolean {
  return typeof content === 'string' && content.startsWith(QA_SUMMARY_PREFIX);
}

export function parseQaSummaryContent(content: string): QaSummaryData | null {
  if (!isQaSummaryContent(content)) return null;
  try {
    const raw = content.slice(QA_SUMMARY_PREFIX.length);
    const parsed = JSON.parse(raw) as QaSummaryData;
    if (!parsed || !Array.isArray(parsed.items)) return null;
    return parsed;
  } catch {
    return null;
  }
}
