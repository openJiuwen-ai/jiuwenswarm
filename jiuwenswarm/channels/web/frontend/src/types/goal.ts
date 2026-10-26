/**
 * Goal（持续目标）类型定义
 *
 * 对应后端 GoalRecord.to_dict()，见 cjh/goal/Goal持续目标Web前端对接.md
 */

export type GoalStatus = 'active' | 'paused' | 'completed' | 'blocked' | string;

export interface GoalTokenUsage {
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  total_tokens: number;
}

// 对应后端 GoalAssessment.to_dict()（openjiuwen/harness/goal/schema.py），真机报文已核实是
// 结构化对象而不是纯字符串，见 progress.md 2026-07-21 真机验证记录。
export interface GoalAssessment {
  status: 'continue' | 'complete' | 'blocked' | string;
  evidence: string;
  remaining_work?: string | null;
  next_instruction?: string | null;
}

export interface GoalRecord {
  goal_id: string;
  session_id: string;
  objective: string;
  status: GoalStatus;
  revision: number;
  attempt_count: number;
  created_at?: string;
  updated_at?: string;
  // 后端已结算的累计耗时（秒）；active_started_at 是当前 active 计时段的开始时间
  // （UTC ISO-8601），非 active 状态下为 null。见 Goal持续目标Web前端对接4.md「前端耗时展示对接」。
  time_used_seconds?: number;
  active_started_at?: string | null;
  token_usage?: GoalTokenUsage;
  max_attempts?: number | null;
  token_budget?: number | null;
  last_assessment?: GoalAssessment | null;
  last_stop_reason?: string | null;
}

export type GoalAction = 'set' | 'pause' | 'resume' | 'clear';
