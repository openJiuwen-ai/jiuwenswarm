export interface JoyAIToolContextEntry {
  jobId: string;
  question: string;
  query: string;
  result: string;
  completedAt: string;
}

export interface JoyAIToolContextBatch {
  text: string;
  jobIds: string[];
}

const MAX_PENDING_TOOL_RESULTS = 4;
const MAX_ATTACHED_TOOL_RESULTS = 4;
const MAX_TOOL_CONTEXT_CHARS = 3_600;

function compact(value: string, maxChars: number): string {
  const normalized = value.replace(/\s+/g, ' ').trim();
  return normalized.length > maxChars ? `${normalized.slice(0, maxChars)}...` : normalized;
}

export function rememberJoyAIToolContext(
  entries: JoyAIToolContextEntry[],
  entry: JoyAIToolContextEntry,
): JoyAIToolContextEntry[] {
  const jobId = entry.jobId.trim();
  const result = entry.result.trim();
  if (!jobId || !result) return entries;
  const normalized = { ...entry, jobId, result };
  return [...entries.filter((item) => item.jobId !== jobId), normalized]
    .slice(-MAX_PENDING_TOOL_RESULTS);
}

export function buildJoyAIToolContextBatch(
  entries: JoyAIToolContextEntry[],
): JoyAIToolContextBatch {
  const candidates = entries.slice(-MAX_ATTACHED_TOOL_RESULTS);
  const selected: Array<{ entry: JoyAIToolContextEntry; block: string }> = [];
  let usedChars = 0;

  for (let index = candidates.length - 1; index >= 0; index -= 1) {
    const entry = candidates[index];
    const block = [
      `原问题：${compact(entry.question, 120) || '未记录'}`,
      `搜索线索：${compact(entry.query, 120) || '未记录'}`,
      `最终结果：${compact(entry.result, 550)}`,
      `完成时间：${entry.completedAt}`,
    ].join('\n');
    const separatorChars = selected.length > 0 ? 2 : 0;
    if (usedChars + separatorChars + block.length > MAX_TOOL_CONTEXT_CHARS) continue;
    selected.unshift({ entry, block });
    usedChars += separatorChars + block.length;
  }

  return {
    text: selected.map((item) => item.block).join('\n\n'),
    jobIds: selected.map((item) => item.entry.jobId),
  };
}

export function removeSentJoyAIToolContext(
  entries: JoyAIToolContextEntry[],
  sentJobIds: string[],
): JoyAIToolContextEntry[] {
  if (sentJobIds.length === 0) return entries;
  const sent = new Set(sentJobIds);
  return entries.filter((entry) => !sent.has(entry.jobId));
}
