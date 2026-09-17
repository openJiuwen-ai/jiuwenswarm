/**
 * ProactiveRecommendationCard 反馈按钮的 className 计算（纯函数，便于单测）。
 *
 * 反馈按钮有三种视觉状态：
 * - 未反馈（null）：半透明底色 + 中等饱和文字，可点。
 * - 已点当前项（like/dislike 与按钮角色一致）：加深底色 + 深色文字，突出选中。
 * - 已点另一项（like/dislike 与按钮角色相反）：整体弱化 + 禁用态。
 *
 * 注意：选中态文字必须使用深色档（green-600 / red-600），不能用 300 档的浅色——
 * 浅色文字在浅色主题的半透明绿色/红色底上对比度过低，会出现「字体不可见」的问题。
 */

export type ProactiveFeedbackState = 'like' | 'dislike' | null;
export type ProactiveFeedbackRole = 'like' | 'dislike';

const BASE_CLASS =
  'flex items-center gap-1 px-2.5 py-1 rounded-full text-xs transition-colors border ';

export function proactiveFeedbackButtonClass(
  state: ProactiveFeedbackState,
  role: ProactiveFeedbackRole,
): string {
  if (role === 'like') {
    if (state === 'like') {
      return BASE_CLASS + 'bg-green-500/30 border-green-500/60 text-green-600';
    }
    if (state === 'dislike') {
      return (
        BASE_CLASS +
        'bg-green-500/10 border-green-500/20 text-green-400/60 opacity-40 cursor-not-allowed'
      );
    }
    return (
      BASE_CLASS +
      'bg-green-500/10 hover:bg-green-500/25 text-green-400 border-green-500/20 hover:border-green-500/40'
    );
  }

  if (state === 'dislike') {
    return BASE_CLASS + 'bg-red-500/30 border-red-500/60 text-red-600';
  }
  if (state === 'like') {
    return (
      BASE_CLASS +
      'bg-red-500/10 border-red-500/20 text-red-400/60 opacity-40 cursor-not-allowed'
    );
  }
  return (
    BASE_CLASS +
    'bg-red-500/10 hover:bg-red-500/25 text-red-400 border-red-500/20 hover:border-red-500/40'
  );
}
