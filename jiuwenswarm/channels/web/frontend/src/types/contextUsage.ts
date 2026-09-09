export interface ContextUsagePart {
  category: string;
  tokens: number;
  percentage_of_window: number | null;
}

/** Fields consumed from the context-usage.v1 post-call event. Ratios are not percentages. */
export interface ContextUsageSnapshot {
  event_type: 'context.usage';
  schema_version: 'context-usage.v1';
  phase: 'post_call';
  request_id: string;
  product_session_id: string;
  /** Team streams identify the main conversation owner semantically with role="leader". */
  role: 'leader' | 'teammate' | null;
  depth: number;
  team_id: string | null;
  member_name: string | null;
  context_window: {
    limit_tokens: number | null;
    input_tokens: number | null;
    occupancy_rate: number | null;
  };
  parts: Record<string, ContextUsagePart>;
  session_kv_cache_hit_rate: number | null;
  /** 用于历史分页恢复时避免旧页覆盖新页；实时事件没有该字段时不参与排序。 */
  timestamp?: string;
}
