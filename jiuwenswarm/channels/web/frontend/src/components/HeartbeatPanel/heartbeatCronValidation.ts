// 心跳 cron 与 CronPanel 使用同一套校验：支持标准 5 段 crontab（分 时 日 月 周）
// 和 7 段格式（秒 分 时 日 月 周 年）。5 段输入补全 second=0、year=* 后复用
// CronPanel 的字段范围规则；7 段输入直接校验，避免两处规则漂移。
// .js 扩展是 Node 原生 ESM 加载器（node --test 使用）的必需项；tsc --module ES2020 不会自动添加扩展名。
import { validateCronExpr } from '../CronPanel/cronExprValidation.js';

export function validateHeartbeatCronExpr(expr: string): { valid: boolean; error?: string } {
  const trimmed = expr.trim();
  const parts = trimmed.split(/\s+/);
  if (parts.length === 5) {
    return validateCronExpr(`0 ${trimmed} *`);
  }
  if (parts.length !== 7) {
    return { valid: false, error: 'heartbeat.errors.cronFieldCount' };
  }
  return validateCronExpr(trimmed);
}
