/**
 * cron 广播事件的会话归属解析（纯函数，便于 node:test 覆盖）。
 *
 * web 通道的 cron 推送不带 payload.session_id：服务端把文件历史落到展示会话
 * （delivery_session_id），桌面端未回写时该字段为空。chat.final 已有
 * exec_session_id / lastRunSessionId 兜底；chat.file 缺同样兜底会把整帧丢弃。
 *
 * 解析顺序：
 *   1. payload.session_id（普通事件 / 已回写的展示会话）
 *   2. payload.cron.exec_session_id（定时任务执行会话）
 *   3. 按 cron.job_id 查最近一次运行会话（立即执行场景）
 */

type RecordLike = Record<string, unknown>

function trimmedString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function asRecord(value: unknown): RecordLike | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as RecordLike)
    : null
}

export function resolveCronEventSessionId(
  payload: RecordLike,
  lookupLastRunSessionId: (jobId: string) => string | undefined
): string | null {
  const direct = trimmedString(payload.session_id)
  if (direct) {
    return direct
  }
  const cron = asRecord(payload.cron)
  if (!cron) {
    return null
  }
  const execSessionId = trimmedString(cron.exec_session_id)
  if (execSessionId) {
    return execSessionId
  }
  const jobId = trimmedString(cron.job_id)
  if (!jobId) {
    return null
  }
  return trimmedString(lookupLastRunSessionId(jobId)) || null
}
