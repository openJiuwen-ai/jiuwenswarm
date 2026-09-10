export interface ToolApprovalDetails {
  toolName: string;
  agentScopeId?: string;
  skillName?: string;
  command: string;
  reason?: string;
  riskLevel?: string;
}

interface ScrollTarget {
  scrollIntoView(options?: ScrollIntoViewOptions): void;
}

function trimCodeFence(value: string): string {
  const trimmed = value.trim();
  const fenced = trimmed.match(/^```[^\n]*\n([\s\S]*?)\n```$/);
  return (fenced?.[1] ?? trimmed).trim();
}

export function parseToolApprovalQuestion(markdown: string): ToolApprovalDetails | null {
  const tool = markdown.match(/\*\*工具\s+`([^`]+)`\s+需要授权才能执行\*\*/);
  const command = markdown.match(/\*\*关键参数或命令：\*\*\s*([\s\S]*?)(?=\n\s*\n\*\*(?:权限原因|风险等级)：|$)/);
  if (!tool || !command) return null;

  const source = markdown.match(/发起方：子 Agent\s+`([^`]+)`(?:（当前 Skill：`([^`]+)`）)?/);
  const reason = markdown.match(/\*\*权限原因：\*\*\s*([\s\S]*?)(?=\n\s*\n\*\*风险等级：|$)/);
  const risk = markdown.match(/\*\*风险等级：([^*]+)\*\*/);
  const result: ToolApprovalDetails = {
    toolName: tool[1].trim(),
    command: trimCodeFence(command[1]),
  };
  if (source?.[1]) result.agentScopeId = source[1].trim();
  if (source?.[2]) result.skillName = source[2].trim();
  if (reason?.[1]) result.reason = reason[1].trim();
  if (risk?.[1]) result.riskLevel = risk[1].trim();
  return result;
}

export function scrollNewApprovalIntoView(target: ScrollTarget | null, requestId: string | undefined, previousRequestId: string | null): string | null {
  if (!requestId || requestId === previousRequestId) return previousRequestId;
  target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  return requestId;
}
