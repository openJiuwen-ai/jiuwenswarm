import { useTranslation } from 'react-i18next';

interface StatusBadgeProps {
  enabled: boolean;
  expired: boolean;
}

// "运行中"图标：lucide 里没有贴近设计稿的对应图标（两个相对方括号），因此手绘还原。
export function RunningIcon({ size = 15 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M3.3 6V5C3.3 3.9 4.2 3 5.3 3H10.7C11.8 3 12.7 3.9 12.7 5V6" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" />
      <path d="M3.3 10V11C3.3 12.1 4.2 13 5.3 13H10.7C11.8 13 12.7 12.1 12.7 11V10" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" />
    </svg>
  );
}

// "已暂停"/"过期"复用的空心圆环图标，同样复刻自 demo：lucide 的 Circle 组件小尺寸下描边圆环会糊成
// 实心点，改成固定比例手绘圆环（外圆 r=6.5，内圆孔洞占比固定）。
export function BoldRingIcon({ size = 12 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="3" />
    </svg>
  );
}

// 这里是"任务本身的状态"（运行中/已暂停/过期），依据 enabled/expired 两个现成字段，跟后端要不要
// 交付执行状态无关——这三态本轮已经完整实现，不是待办（2026-07-14 用户已澄清并纠正早前文档里
// "运行失败也是任务列表三态之一"的误解）。"运行失败"属于"执行历史"维度（某一次执行的结果：
// 运行成功/运行失败/手动停止），不会出现在这个组件里，见 CronPanel/index.tsx 的执行历史 tab
// （目前用 CRON_HISTORY_UI_ENABLED 隐藏，等 backend-requests.md 需求1 交付后再接真实数据）。
// 图标复刻自阶段1 demo 的 __demo__/cron-tasks/StatusBadge.tsx。
export default function StatusBadge({ enabled, expired }: StatusBadgeProps) {
  const { t } = useTranslation();
  if (expired) {
    return (
      <span className="inline-flex items-center gap-1.5 text-sm text-amber-600">
        <BoldRingIcon />
        {t('cron.status.expired')}
      </span>
    );
  }
  if (enabled) {
    return (
      <span className="inline-flex items-center gap-1.5 text-sm text-cron-running">
        <RunningIcon />
        {t('cron.status.running')}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-sm text-text-muted">
      <BoldRingIcon />
      {t('cron.status.paused')}
    </span>
  );
}
