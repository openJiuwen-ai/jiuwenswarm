// 心跳的 cron 计划用标准 5 段 crontab（分 时 日 月 周），跟 CronPanel 内部使用的 7 段 croniter
// 表达式（含 second/year）不是同一种格式，见接口规格说明 §4.1。校验时补全 second=0、year=* 后
// 复用 CronPanel 现成的字段范围规则（validateCronExpr），避免维护两套 min/max 规则并让两处校验
// 结果不一致。
// .js 扩展是 Node 原生 ESM 加载器（node --test 使用）的必需项；tsc --module ES2020 不会自动添加扩展名。
import { validateCronExpr } from '../CronPanel/cronExprValidation.js';

export function validateHeartbeatCronExpr(expr: string): { valid: boolean; error?: string } {
  const trimmed = expr.trim();
  const parts = trimmed.split(/\s+/);
  if (parts.length !== 5) {
    return { valid: false, error: 'heartbeat.errors.cronFieldCount' };
  }
  return validateCronExpr(`0 ${trimmed} *`);
}
