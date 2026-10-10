// 后端 `_inject_swarmflow_context` 会把本轮 leader 裁决用的恢复建议以锚点包裹、
// 前缀拼进 user turn 文本。这段悄悄话是给当轮 leader 看的，不应在历史渲染里
// 露给用户（用户会看到自己的消息被贴了机器注入的前缀），渲染前按锚点剔除。
const SWARMFLOW_ADVISORY_SPAN = /\[swarmflow-advisory\][\s\S]*?\[\/swarmflow-advisory\]\s*/g;

/** 剔除 [swarmflow-advisory] ... [/swarmflow-advisory] 段（含两个标记与段尾空白）。 */
export function stripSwarmflowAdvisory(text: string): string {
  if (!text || !text.includes('[swarmflow-advisory]')) {
    return text;
  }
  return text.replace(SWARMFLOW_ADVISORY_SPAN, '');
}
