import type { SearchJobPayload, SearchProgressJob } from './types';

function cleanModelText(text: string): string {
  return text
    .replace(/<think>[\s\S]*?<\/think>/gi, '')
    .replace(/<\/?think>/gi, '')
    .trim();
}

interface SearchStatusItem {
  status: 'running' | 'queued' | 'failed';
}

export function searchAwareToolStatus(
  status: string,
  jobs: Iterable<SearchStatusItem>,
): string {
  const foreground = status.trim();
  const runningCount = [...jobs].filter((job) => job.status === 'running').length;
  if (runningCount === 0) return foreground;

  const background = `${runningCount} 项正在后台搜索，可继续提问…`;
  if (!foreground || /后台搜索|正在使用.+搜索/.test(foreground)) {
    return runningCount === 1 ? '正在后台搜索，可继续提问…' : background;
  }
  return `${foreground.replace(/[；。…]+$/u, '')}；另有 ${background}`;
}

export function assistantSpeechText(text: string, maxChars = 180): string {
  const normalized = cleanModelText(text)
    .replace(/\[([^\]]+)]\(https?:\/\/[^)]+\)/g, '$1')
    .replace(/https?:\/\/\S+/g, '')
    .replace(/\s*\[来源\d+\]/g, '')
    .replace(/[*_#>`~]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (normalized.length <= maxChars) return normalized;
  const prefix = normalized.slice(0, maxChars);
  const sentenceEnd = Math.max(
    prefix.lastIndexOf('。'),
    prefix.lastIndexOf('！'),
    prefix.lastIndexOf('？'),
  );
  if (sentenceEnd >= Math.floor(maxChars * 0.6)) return prefix.slice(0, sentenceEnd + 1);
  return `${prefix.replace(/[，、；：,.!?\s]+$/g, '')}。`;
}

export const MAX_SEARCH_PROGRESS_JOBS = 8;

export function mergeSearchProgressJob(
  jobs: SearchProgressJob[],
  payload: SearchJobPayload,
): SearchProgressJob[] {
  const jobId = payload.job_id?.trim();
  if (!jobId) return jobs;

  const existingIndex = jobs.findIndex((job) => job.id === jobId);
  const existing = existingIndex >= 0 ? jobs[existingIndex] : undefined;
  const incoming = payload.progress_history?.length
    ? payload.progress_history
    : payload.progress ? [payload.progress] : [];
  const progressByKey = new Map(
    (existing?.progress || []).map((entry) => [
      `${entry.sequence}:${entry.stage}:${entry.tool_call_id || ''}`,
      entry,
    ]),
  );
  incoming.forEach((entry) => {
    progressByKey.set(`${entry.sequence}:${entry.stage}:${entry.tool_call_id || ''}`, entry);
  });

  const updated: SearchProgressJob = {
    id: jobId,
    query: payload.query?.trim() || existing?.query || '',
    status: payload.status || existing?.status || 'running',
    latencyMs: payload.latency_ms ?? existing?.latencyMs,
    progress: [...progressByKey.values()].sort((left, right) => left.sequence - right.sequence),
  };

  const nextJobs = existingIndex >= 0
    ? jobs.map((job, index) => index === existingIndex ? updated : job)
    : [...jobs, updated];
  return nextJobs.slice(-MAX_SEARCH_PROGRESS_JOBS);
}

export function selectSearchProgressJob(
  jobs: SearchProgressJob[],
  selectedJobId: string,
): SearchProgressJob | undefined {
  return jobs.find((job) => job.id === selectedJobId) ?? jobs.at(-1);
}

export function searchProgressOptionLabel(job: SearchProgressJob, position: number): string {
  const query = job.query.replace(/\s+/g, ' ').trim() || '未命名搜索';
  const summary = query.length > 26 ? `${query.slice(0, 26)}...` : query;
  const status = job.status === 'running' ? '进行中' : job.status === 'completed' ? '已完成' : '失败';
  return `${position}. ${summary} (${status})`;
}
