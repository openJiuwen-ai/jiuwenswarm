import type { CronTemplateUI } from '../../types/cron';

// 沿用旧 CronPanel/index.tsx 的时区选项列表
export const TIMEZONE_OPTIONS = [
  'Asia/Shanghai',
  'Asia/Bangkok',
  'Asia/Tokyo',
  'Asia/Seoul',
  'Asia/Singapore',
  'Europe/London',
  'Europe/Paris',
  'America/New_York',
  'America/Los_Angeles',
  'America/Chicago',
];

// 任务模板：后端没有"模板"概念（见 _migration/backend-requests.md），本轮先用前端静态常量，
// 不做后端持久化/用户自定义模板。cron_expr 只是预填的初始值，用户可在"Cron表达式"输入框里改。
// 周字段是 croniter 实测的真实编号：0=周日...6=周六（不是 Quartz 的 1=SUN...7=SAT），见
// cronExprValidation.ts 顶部说明；"工作周报"模板曾按 Quartz 惯例误写成 6（周六），实际描述文案
// 写的是"每周五"，2026-07-14 用户验收发现并订正为 5（周五）。
export const CRON_TEMPLATES: CronTemplateUI[] = [
  {
    id: 'tpl-daily-news',
    icon: 'trend',
    titleKey: 'cron.template.trend.title',
    descriptionKey: 'cron.template.trend.description',
    cronExpr: '0 0 8 * * ? *',
  },
  {
    id: 'tpl-market-watch',
    icon: 'newspaper',
    titleKey: 'cron.template.newspaper.title',
    descriptionKey: 'cron.template.newspaper.description',
    cronExpr: '0 0 12 * * ? *',
  },
  {
    id: 'tpl-weekly-report',
    icon: 'briefcase',
    titleKey: 'cron.template.briefcase.title',
    descriptionKey: 'cron.template.briefcase.description',
    cronExpr: '0 0 18 ? * 5 *',
  },
];
