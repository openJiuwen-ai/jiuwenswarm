import type { AgentMode } from './index';

/** 后端 cron.job.* 系列 RPC 实际收发的字段，对齐 jiuwenswarm/gateway/cron/models.py 的 CronJob.to_dict() */
export interface CronJobDTO {
  id: string;
  name: string;
  enabled: boolean;
  expired: boolean;
  cron_expr: string;
  timezone: string;
  wake_offset_seconds: number;
  description: string;
  targets: string;
  created_at: number | null;
  updated_at: number | null;
  session_id?: string;
  chat_type?: string;
  mode?: string;
  delete_after_run?: boolean;
  timeout_seconds?: number;
  project_id: string;
  last_session_id?: string;
  model_name?: string;
}

/** UI 层展示用结构，来自 CronJobDTO 派生（见 cronJobToUI） */
export interface CronTaskUI {
  id: string;
  name: string;
  projectId: string;
  projectName: string | null;
  description: string;
  modelName: string | null;
  /** 执行模式：单Agent('agent')/集群('team')，从后端 mode 归一而来，见 CronPanel/index.tsx cronJobToUI */
  mode: AgentMode;
  cronExpr: string;
  timezone: string;
  /** 提前唤醒秒数：在计划推送时间前启动 Agent；0 表示到点执行 */
  wakeOffsetSeconds: number;
  enabled: boolean;
  expired: boolean;
  deliveryChannel: string;
}

export interface CronTemplateUI {
  id: string;
  icon: 'trend' | 'newspaper' | 'briefcase';
  titleKey: string;
  descriptionKey: string;
  cronExpr: string;
}

/** 执行计划编辑器（ScheduleEditor）内部结构化状态，见 _migration/plan.md §2.3.6/§2.3.8 */
export type CronScheduleKind = 'daily' | 'weekly' | 'monthly' | 'monthlyWeekday' | 'yearly' | 'interval' | 'once';

export interface CronSchedule {
  kind: CronScheduleKind;
  time?: string; // HH:mm，daily/weekly/monthly/monthlyWeekday/yearly/once 用
  weekdays?: number[]; // 0=周日...6=周六（croniter 真实编号），weekly/interval/monthlyWeekday 用
  day?: number | 'L'; // monthly 用："每月第几天"，或 'L' 表示月末最后一天
  weekOfMonth?: number | 'L'; // monthlyWeekday 用：1-4，或 'L' 表示最后一周（不支持"第五周"，见 §2.3.8）
  month?: number; // 1-12，yearly 用
  date?: string; // YYYY-MM-DD，once 用
  intervalUnit?: 'hours' | 'minutes'; // interval 用：数字的单位，默认 'hours'（兼容旧数据/旧生成结果）
  everyHours?: number; // interval 用，intervalUnit 为 'hours' 时的取值；只允许正整数（croniter 小时步长不支持小数）
  everyMinutes?: number; // interval 用，intervalUnit 为 'minutes' 时的取值；只允许正整数且需整除 60
}
