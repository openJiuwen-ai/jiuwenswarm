/**
 * Display models of the diagnosis sub-page: stage list, record list rows and
 * the diagnosed target.
 *
 * Kept free of React and browser APIs so it can be tested with node --test.
 */

import { isTtseCategory } from './diagnosisState';
import type {
  DiagnosisRecord,
  DiagnosisStage,
  DiagnosisStatus,
  DiagnosisSummary,
  RewardPayload,
  RootCause,
  SignalRef,
  StageStatus,
} from './diagnosisTypes';

/** Stages the engine runs, in order. Shown as waiting until the engine starts them. */
export const PLANNED_STAGE_IDS = ['task_parse', 'diagnose', 'attribute'] as const;

export type StageViewStatus = StageStatus | 'pending';

export interface StageView {
  stageId: string;
  status: StageViewStatus;
  /** The engine's stage, or `null` for a waiting placeholder. */
  stage: DiagnosisStage | null;
}

/**
 * Stages to draw for a record.
 *
 * While the diagnosis runs, planned stages the engine has not started yet
 * appear as waiting placeholders, and stages outside the plan follow the
 * planned ones. A finished diagnosis lists exactly what the engine reported.
 */
export function stageViews(stages: readonly DiagnosisStage[], running: boolean): StageView[] {
  if (!running) {
    return stages.map(stage => ({ stageId: stage.stage_id, status: stage.status, stage }));
  }
  const byId = new Map(stages.map(stage => [stage.stage_id, stage] as const));
  const planned = new Set<string>(PLANNED_STAGE_IDS);
  const views: StageView[] = PLANNED_STAGE_IDS.map((stageId) => {
    const stage = byId.get(stageId);
    return stage === undefined
      ? { stageId, status: 'pending', stage: null }
      : { stageId, status: stage.status, stage };
  });
  for (const stage of stages) {
    if (!planned.has(stage.stage_id)) {
      views.push({ stageId: stage.stage_id, status: stage.status, stage });
    }
  }
  return views;
}

/**
 * Whether a stage body is open.
 *
 * Without a user choice, every stage is open while the diagnosis runs, so
 * the whole process can be followed; once it is over, completed stages fold
 * to their summary line while failed and skipped ones stay open. A waiting
 * stage has no body.
 */
export function isStageExpanded(
  status: StageViewStatus,
  userChoice: boolean | undefined,
  running: boolean,
): boolean {
  if (status === 'pending') return false;
  if (userChoice !== undefined) return userChoice;
  return running || status !== 'completed';
}

export type StatusIconKind = 'running' | 'completed' | 'failed';

/** Icon of a record in the record list: cancelled and interrupted count as unsuccessful. */
export function recordIconKind(status: DiagnosisStatus): StatusIconKind {
  if (status === 'running') return 'running';
  return status === 'completed' ? 'completed' : 'failed';
}

function pad(value: number, width = 2): string {
  return String(value).padStart(width, '0');
}

function dateOf(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function clockOf(date: Date): string {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** `YYYY-MM-DD HH:mm:ss` in local time, or an empty string for a missing or invalid timestamp. */
export function formatRecordTime(value: string | undefined): string {
  if (value === undefined || value === '') return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return `${dateOf(date)} ${clockOf(date)}`;
}

/** Second line of a record row: the engine's headline, else the scope kind. */
export function recordLabel(summary: Pick<DiagnosisSummary, 'headline' | 'scope_kind'>, scopeLabel: string): string {
  return summary.headline === undefined || summary.headline === '' ? scopeLabel : summary.headline;
}

/** Records whose time or label contains every word of the query, case-insensitive. */
export function filterRecords(
  items: readonly DiagnosisSummary[],
  query: string,
  scopeLabelOf: (kind: DiagnosisSummary['scope_kind']) => string,
): DiagnosisSummary[] {
  const words = query.toLowerCase().split(/\s+/).filter(word => word !== '');
  if (words.length === 0) return [...items];
  return items.filter((item) => {
    const haystack = `${formatRecordTime(item.created_at)} ${recordLabel(item, scopeLabelOf(item.scope_kind))}`
      .toLowerCase();
    return words.every(word => haystack.includes(word));
  });
}

/** Unix nanoseconds, as the API carries them, to Unix milliseconds; `null` when malformed. */
export function unixNanoToMs(value: string): number | null {
  if (!/^\d+$/.test(value)) return null;
  const ms = Number(BigInt(value) / BigInt(1_000_000));
  return Number.isFinite(ms) ? ms : null;
}

export interface DiagnosisTargetGroup {
  startMs: number;
  endMs: number;
  subjectIds: string[];
}

export interface DiagnosisTarget {
  /** The whole trajectory was diagnosed, so there is no window to show. */
  full: boolean;
  /** Agents sharing one window; a single group when the selection came from the time axis. */
  groups: DiagnosisTargetGroup[];
}

/** What a record diagnosed: the whole trajectory, or windows grouped by identical time span. */
export function diagnosisTarget(record: Pick<DiagnosisRecord, 'input'>): DiagnosisTarget {
  const scope = record.input.scope;
  if (scope.kind === 'full') return { full: true, groups: [] };
  const groups: DiagnosisTargetGroup[] = [];
  for (const range of scope.ranges) {
    const startMs = unixNanoToMs(range.start_unix_nano);
    const endMs = unixNanoToMs(range.end_unix_nano);
    if (startMs === null || endMs === null) continue;
    const group = groups.find(item => item.startMs === startMs && item.endMs === endMs);
    if (group === undefined) groups.push({ startMs, endMs, subjectIds: [range.subject_id] });
    else group.subjectIds.push(range.subject_id);
  }
  return { full: false, groups };
}

/** `YYYY-MM-DD HH:mm:ss – HH:mm:ss`; the end repeats the date only when it falls on another day. */
export function formatTimeWindow(startMs: number, endMs: number): string {
  const start = new Date(startMs);
  const end = new Date(endMs);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return '';
  const sameDay = dateOf(start) === dateOf(end);
  return `${dateOf(start)} ${clockOf(start)} – ${sameDay ? '' : `${dateOf(end)} `}${clockOf(end)}`;
}

/** Reward tiers, with the thresholds agent-core uses: 90 and above, 75 and above, the rest. */
export type RewardTier = 'perfect' | 'fair' | 'weak';

export function rewardTier(score: number): RewardTier {
  if (score >= 90) return 'perfect';
  return score >= 75 ? 'fair' : 'weak';
}

/** Tier label key of a signal: the engine's tier for Reward signals, none for rule signals. */
export function signalTier(signal: SignalRef): RewardTier | null {
  if (signal.source !== 'reward') return null;
  if (signal.tier === 'perfect' || signal.tier === 'fair' || signal.tier === 'weak') return signal.tier;
  return typeof signal.score === 'number' ? rewardTier(signal.score) : null;
}

/** Split a Reward signal text, "requirement\ncomment", at the first line break. */
export function splitRewardSignalText(text: string): { requirement: string; comment: string } {
  const index = text.indexOf('\n');
  if (index < 0) return { requirement: text.trim(), comment: '' };
  return { requirement: text.slice(0, index).trim(), comment: text.slice(index + 1).trim() };
}

/**
 * Split Markdown at its first blank line into a lead paragraph and the rest.
 *
 * Headings the model writes inside are left alone; only blank lines divide.
 * A single paragraph has an empty rest and needs no folding.
 */
export function splitLeadParagraph(text: string): { lead: string; rest: string } {
  const trimmed = text.trim();
  const match = /\n[ \t]*\n/.exec(trimmed);
  if (match === null) return { lead: trimmed, rest: '' };
  return { lead: trimmed.slice(0, match.index).trim(), rest: trimmed.slice(match.index + match[0].length).trim() };
}

/**
 * Drop a heading the model opened its Markdown with. A suggestion card already says what it is
 * about in its top row, so a leading "## ... 改进意见" only repeats it. A heading that is all there
 * is stays, and headings further down are left alone.
 */
export function stripLeadingHeading(text: string): string {
  const trimmed = text.trim();
  const match = /^#{1,6}[ \t]+[^\n]*\n/.exec(trimmed);
  if (match === null) return trimmed;
  const rest = trimmed.slice(match[0].length).trim();
  return rest === '' ? trimmed : rest;
}

/** Whether to draw the category badge: `other` without a confidence says nothing. */
export function showCategoryBadge(cause: Pick<RootCause, 'category' | 'confidence'>): boolean {
  if (isTtseCategory(cause.category)) return false;
  return !(cause.category === 'other' && (cause.confidence === undefined || cause.confidence === null));
}

/**
 * The findings of a diagnosis in reading order: the attribution root causes as they were reported,
 * then the experience verdict on the whole stretch, which only arrives once attribution is done.
 * Every finding is one root cause.
 */
export function orderFindings(causes: readonly RootCause[]): RootCause[] {
  return [
    ...causes.filter(cause => !isTtseCategory(cause.category)),
    ...causes.filter(cause => isTtseCategory(cause.category)),
  ];
}

export interface FindingCounts {
  /** Problems the attribution located; this is the number the headline reports. */
  issues: number;
  /** Experience verdicts, which judge the whole stretch and are not problems. */
  experience: number;
}

/** How many of the findings are problems and how many are experience verdicts. */
export function findingCounts(causes: readonly RootCause[]): FindingCounts {
  const experience = causes.filter(cause => isTtseCategory(cause.category)).length;
  return { issues: causes.length - experience, experience };
}

/**
 * Break an evaluator comment into the points it makes, one per line or per full-width semicolon.
 * Only those two break a comment, so text that merely contains `;` (commands, code) stays whole.
 */
export function commentPoints(text: string): string[] {
  return text
    .split(/\r?\n|；/)
    .map(point => point.replace(/^\s*[-*•]\s+/, '').trim())
    .filter(point => point !== '');
}

/** What the lead tag of a finding says: the experience verdict, or how its signal fared. */
export type FindingTag =
  | { kind: 'experience'; tier: RewardTier; outcome: string }
  | { kind: 'signal'; tier: RewardTier | null; source: string | null };

/** The lead tag of a finding; a Reward signal carries a tier, a rule signal only its source. */
export function findingTag(cause: Pick<RootCause, 'category' | 'signal'>): FindingTag | null {
  if (isTtseCategory(cause.category)) {
    return { kind: 'experience', tier: ttseTier(cause.category), outcome: cause.category.slice('ttse_'.length) };
  }
  const signal = cause.signal ?? null;
  if (signal === null) return null;
  const known = signal.source === 'keyword_tool' || signal.source === 'keyword_user';
  return { kind: 'signal', tier: signalTier(signal), source: known ? signal.source : null };
}

export interface FindingEvidence {
  /** Requirement a Reward signal was scored against; empty for a rule signal. */
  requirement: string;
  /** The signal text a finding stands on: the Reward comment, or the raw rule hit. */
  text: string;
}

/** The signal behind a finding, or `null` when it has none (the experience verdict). */
export function findingEvidence(cause: Pick<RootCause, 'evidence' | 'signal'>): FindingEvidence | null {
  if (cause.evidence.trim() === '') return null;
  if (cause.signal?.source === 'reward') {
    const { requirement, comment } = splitRewardSignalText(cause.evidence);
    return { requirement, text: comment };
  }
  return { requirement: '', text: cause.evidence };
}

/** Root cause range, "main:23 → main:30", or a single node; empty when no node ids exist. */
export function causeNodeLabel(cause: Pick<RootCause, 'refs'>): string {
  const ids: string[] = [];
  for (const ref of cause.refs) {
    if (ref.node_id !== undefined && ref.node_id !== '' && !ids.includes(ref.node_id)) ids.push(ref.node_id);
  }
  return ids.join(' → ');
}

/** Elapsed time of a stage, "850ms", "20.4s" or "1m 5s"; empty when unknown. */
export function formatElapsed(ms: number | undefined): string {
  if (ms === undefined || !Number.isFinite(ms) || ms < 0) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const seconds = Math.round(ms / 1000);
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

/** Whether a Skill key reads as a file path rather than a Skill name. */
export function looksLikePath(key: string): boolean {
  return key.includes('/') || key.includes('\\') || key.toLowerCase().endsWith('.md');
}

export type SuggestionsState = 'pending' | 'unfinished' | 'ready';

/** What the suggestion card can say: still running, ended without a result, or finished. */
export function suggestionsState(status: DiagnosisStatus): SuggestionsState {
  if (status === 'running') return 'pending';
  return status === 'completed' ? 'ready' : 'unfinished';
}

const LLM_LINE = /^(llm-\d+) · /;

export interface StageOutputView {
  /** Output lines that are not model request status lines. */
  body: string;
  /** Model request status lines, as written. */
  requestLines: string[];
  /** Distinct model requests behind those lines. */
  requestCount: number;
}

/** Separate model request status lines ("llm-3 · ...") from the rest of a stage's output. */
export function splitStageOutput(output: string): StageOutputView {
  const body: string[] = [];
  const requestLines: string[] = [];
  const requests = new Set<string>();
  for (const line of output.split('\n')) {
    const match = LLM_LINE.exec(line);
    if (match === null) {
      body.push(line);
    } else {
      requestLines.push(line);
      requests.add(match[1]);
    }
  }
  return { body: body.join('\n').trim(), requestLines, requestCount: requests.size };
}

/** Tone of a TTSE experience verdict: success reads like a met tier, failure like a missed one. */
export function ttseTier(category: string): RewardTier {
  if (category === 'ttse_success') return 'perfect';
  return category === 'ttse_partial' ? 'fair' : 'weak';
}

export interface ProcessSummary {
  /** Stages the engine reported. */
  count: number;
  /** Time the stages took together; empty when no stage has a duration yet. */
  elapsed: string;
}

/** One-line summary of the process: how many stages ran and how long they took. */
export function processSummary(stages: readonly DiagnosisStage[]): ProcessSummary {
  const timed = stages.filter(stage => stage.elapsed_ms !== undefined);
  const total = timed.reduce((sum, stage) => sum + (stage.elapsed_ms ?? 0), 0);
  return { count: stages.length, elapsed: timed.length === 0 ? '' : formatElapsed(total) };
}

/** One scored dimension of a category. */
export interface RewardDimensionScore {
  requirement: string;
  comment: string;
  score: number;
  tier: RewardTier;
}

export interface RewardCategoryScore {
  name: string;
  /** `null` for a category the evaluator gave dimensions but no score of its own. */
  score: number | null;
  tier: RewardTier | null;
  /** The evaluator's comment on the category. */
  comment: string;
  dimensions: RewardDimensionScore[];
}

export interface RewardOverview {
  totalScore: number;
  comment: string;
  /** Categories in the evaluator's order: those with a score, then any that only own dimensions. */
  categories: RewardCategoryScore[];
  /** Dimensions the evaluator scored, and how many of them are perfect and need no attribution. */
  dimensionCount: number;
  perfectCount: number;
}

/**
 * What the conclusion shows of a Reward evaluation: total, comment, and per category its score,
 * comment and dimensions, which the reader opens one category at a time.
 */
export function rewardOverview(reward: RewardPayload): RewardOverview {
  const dimensionsOf = (name: string): RewardDimensionScore[] => reward.dimensions
    .filter(item => item.category === name)
    .map(item => ({
      requirement: item.requirement.trim(),
      comment: item.comment.trim(),
      score: item.score,
      tier: rewardTier(item.score),
    }));
  const categories: RewardCategoryScore[] = [];
  for (const item of reward.categories) {
    const dimensions = dimensionsOf(item.name);
    if (typeof item.score !== 'number' && dimensions.length === 0) continue;
    categories.push({
      name: item.name,
      score: typeof item.score === 'number' ? item.score : null,
      tier: typeof item.score === 'number' ? rewardTier(item.score) : null,
      comment: (item.comment ?? '').trim(),
      dimensions,
    });
  }
  const known = new Set(reward.categories.map(item => item.name));
  for (const name of new Set(reward.dimensions.map(item => item.category))) {
    if (known.has(name)) continue;
    categories.push({ name, score: null, tier: null, comment: '', dimensions: dimensionsOf(name) });
  }
  return {
    totalScore: reward.overall.total_score,
    comment: reward.overall.comment.trim(),
    categories,
    dimensionCount: reward.dimensions.length,
    perfectCount: reward.dimensions.filter(item => rewardTier(item.score) === 'perfect').length,
  };
}