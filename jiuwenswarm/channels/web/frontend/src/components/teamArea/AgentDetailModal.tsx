/**
 * Agent 详情弹窗（共享组件）。
 *
 * 缩进树（SwarmflowTreeView）与架构图（SwarmflowGraphView）共用，
 * 保证两处的输入/输出/错误等内容展示完全一致。
 *
 * 功能：
 * - 标题显示 Agent 名 + 当前 section
 * - Tab 栏可在输入/提问/回复/输出/错误间切换，无需反复开关
 * - 内容用 MarkdownRenderer 渲染
 * - Esc / 点击遮罩关闭
 */

import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { MarkdownRenderer } from '../MarkdownRenderer';
import type { WorkflowAgent } from './workflowTypes';

// ── 字数格式化 ────────────────────────────────────────────

export function formatCharCount(s: string): string {
  const n = s.length;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return `${n}`;
}

// ── 详情 Section 类型 ─────────────────────────────────────

export type DetailSectionKey = 'prompt' | 'human_prompt' | 'human_reply' | 'outcome' | 'error' | 'result';
export type DetailAccent = 'blue' | 'amber' | 'emerald' | 'red';
export interface DetailSection {
  key: DetailSectionKey;
  label: string;
  icon: string;
  content: string;
  accent: DetailAccent;
}

export interface AgentModalState {
  sections: DetailSection[];
  activeKey: DetailSectionKey;
}

export const accentTextClass: Record<DetailAccent, string> = {
  blue: 'text-blue-600 dark:text-blue-300',
  amber: 'text-amber-600 dark:text-amber-300',
  emerald: 'text-emerald-600 dark:text-emerald-300',
  red: 'text-red-600 dark:text-red-300',
};

export const accentChipClass: Record<DetailAccent, string> = {
  blue: 'border-blue-500/40 bg-blue-500/10 text-blue-600 dark:text-blue-300 hover:bg-blue-500/20',
  amber: 'border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-300 hover:bg-amber-500/20',
  emerald: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300 hover:bg-emerald-500/20',
  red: 'border-red-500/40 bg-red-500/10 text-red-600 dark:text-red-300 hover:bg-red-500/20',
};

export const accentTabActiveClass: Record<DetailAccent, string> = {
  blue: 'border-blue-500 text-blue-600 dark:text-blue-300 bg-blue-500/10',
  amber: 'border-amber-500 text-amber-600 dark:text-amber-300 bg-amber-500/10',
  emerald: 'border-emerald-500 text-emerald-600 dark:text-emerald-300 bg-emerald-500/10',
  red: 'border-red-500 text-red-600 dark:text-red-300 bg-red-500/10',
};

/** 根据 agent 字段汇总可用的详情 section，标签与弹窗共用同一数据源。 */
export function buildDetailSections(agent: WorkflowAgent): DetailSection[] {
  const secs: DetailSection[] = [];
  if (agent.prompt) secs.push({ key: 'prompt', label: '输入', icon: '▶', content: agent.prompt, accent: 'blue' });
  if (agent.human_prompt) secs.push({ key: 'human_prompt', label: '人工提问', icon: '☺', content: agent.human_prompt, accent: 'amber' });
  if (agent.human_reply) secs.push({ key: 'human_reply', label: '人工回复', icon: '✓', content: agent.human_reply, accent: 'emerald' });
  if (agent.outcome) secs.push({ key: 'outcome', label: '输出', icon: '◀', content: agent.outcome, accent: 'emerald' });
  if (agent.error) secs.push({ key: 'error', label: '错误', icon: '✕', content: agent.error, accent: 'red' });
  return secs;
}

// ── JSON 树视图 ───────────────────────────────────────────

/** 尝试把内容解析为 JSON 对象/数组，失败返回 null。 */
function tryParseJson(content: string): unknown | null {
  const trimmed = content.trim();
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return null;
  try {
    const parsed = JSON.parse(trimmed);
    return typeof parsed === 'object' && parsed !== null ? parsed : null;
  } catch {
    return null;
  }
}

function JsonValue({ value }: { value: unknown }) {
  if (value === null) return <span className="text-text">null</span>;
  if (typeof value === 'string') return <span className="text-text">&quot;{value}&quot;</span>;
  if (typeof value === 'number') return <span className="text-text">{value}</span>;
  if (typeof value === 'boolean') return <span className="text-text">{String(value)}</span>;
  return <span className="text-text">{String(value)}</span>;
}

function JsonNode({ name, value, depth }: { name?: string; value: unknown; depth: number }) {
  const isContainer = value !== null && typeof value === 'object';
  const entries: [string, unknown][] = isContainer
    ? Array.isArray(value)
      ? value.map((v, i) => [String(i), v] as [string, unknown])
      : Object.entries(value as Record<string, unknown>)
    : [];
  const [collapsed, setCollapsed] = useState(depth >= 1 && entries.length > 10);

  if (!isContainer) {
    return (
      <div className="py-0.5">
        {name !== undefined && (
          <>
            <span className="text-blue-600 dark:text-blue-400">{name}</span>
            <span className="text-text-muted">: </span>
          </>
        )}
        <JsonValue value={value} />
      </div>
    );
  }

  const open = Array.isArray(value) ? '[' : '{';
  const close = Array.isArray(value) ? ']' : '}';

  return (
    <div className="py-0.5">
      <button
        type="button"
        onClick={() => setCollapsed(!collapsed)}
        className="inline-flex items-center gap-0.5 hover:bg-secondary/40 rounded px-0.5"
      >
        <span className="text-text-muted text-[10px] w-3 inline-block">{collapsed ? '▶' : '▼'}</span>
        {name !== undefined && (
          <>
            <span className="text-blue-600 dark:text-blue-400">{name}</span>
            <span className="text-text-muted">: </span>
          </>
        )}
        <span className="text-text-muted">{open}</span>
        {collapsed && (
          <span className="text-text-muted text-xs">
            {entries.length} {Array.isArray(value) ? 'items' : 'keys'}{close}
          </span>
        )}
      </button>
      {!collapsed && (
        <div className="ml-3 border-l border-border/30 pl-2">
          {entries.map(([k, v]) => (
            <JsonNode key={k} name={k} value={v} depth={depth + 1} />
          ))}
          <div className="text-text-muted">{close}</div>
        </div>
      )}
    </div>
  );
}

function JsonTreeView({ data }: { data: unknown }) {
  return (
    <div className="font-mono text-xs leading-relaxed">
      <JsonNode value={data} depth={0} />
    </div>
  );
}

// ── 弹窗组件 ──────────────────────────────────────────────

export interface AgentDetailModalProps {
  state: AgentModalState | null;
  agentName: string;
  onClose: () => void;
  onTabChange: (key: DetailSectionKey) => void;
}

export function AgentDetailModal({ state, agentName, onClose, onTabChange }: AgentDetailModalProps) {
  const { t } = useTranslation();
  const [rawMode, setRawMode] = useState(false);

  useEffect(() => {
    if (!state) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [state, onClose]);

  // 切换 section 时重置为格式化模式
  useEffect(() => {
    setRawMode(false);
  }, [state?.activeKey]);

  if (!state) return null;

  const activeSection = state.sections.find((s) => s.key === state.activeKey);
  const content = activeSection?.content ?? '';
  // 仅对输入/输出尝试 JSON 渲染
  const isJsonSection = activeSection?.key === 'prompt' || activeSection?.key === 'outcome';
  // 错误/结果为纯文本（traceback / 摘要），不套 Markdown
  const isPlainTextSection = activeSection?.key === 'error' || activeSection?.key === 'result';
  const jsonData = isJsonSection ? tryParseJson(content) : null;

  return (
    <div
      className="fixed inset-0 z-[2200] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
    >
      <button
        type="button"
        className="absolute inset-0 bg-black/60"
        onClick={onClose}
        aria-label="关闭"
      />
      <div className="relative w-full max-w-4xl max-h-[85vh] overflow-hidden rounded-xl border border-border bg-card shadow-2xl animate-rise flex flex-col">
        {/* 标题：Agent 名 · 当前 section label */}
        <div className="flex items-center justify-between gap-3 px-5 py-3 border-b border-border bg-panel shrink-0">
          <div className="min-w-0 flex-1">
            <h3 className="text-base font-semibold text-text truncate">{agentName}</h3>
            <p className="text-xs text-text-muted mt-0.5">{activeSection?.label}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="px-2.5 py-1.5 rounded-md border border-border bg-secondary/50 text-text-muted hover:text-text hover:bg-secondary text-sm shrink-0"
          >
            {t('common.close')}
          </button>
        </div>

        {/* Tab 栏：可在输入/提问/回复/输出/错误间切换，无需反复开关 */}
        {state.sections.length > 1 && (
          <div className="flex items-center gap-1 px-3 py-2 border-b border-border/60 bg-secondary/20 shrink-0 overflow-x-auto">
            {state.sections.map((sec) => {
              const active = sec.key === state.activeKey;
              return (
                <button
                  key={sec.key}
                  type="button"
                  onClick={() => onTabChange(sec.key)}
                  className={`shrink-0 inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ${
                    active
                      ? accentTabActiveClass[sec.accent]
                      : 'border-transparent text-text-muted hover:text-text hover:bg-secondary/60'
                  }`}
                >
                  <span>{sec.icon}</span>
                  <span>{sec.label}</span>
                  <span className="text-[9px] tabular-nums opacity-70">{formatCharCount(sec.content)}</span>
                </button>
              );
            })}
          </div>
        )}

        {/* 内容区：JSON 树 / Markdown 自适应 + 原始切换 */}
        <div className="p-5 overflow-auto flex-1">
          {jsonData !== null && (
            <div className="flex justify-end gap-1 mb-2">
              <button
                type="button"
                onClick={() => setRawMode(false)}
                className={`px-2 py-0.5 rounded text-[11px] border transition-colors ${
                  !rawMode
                    ? 'border-blue-500/50 bg-blue-500/10 text-blue-600 dark:text-blue-300'
                    : 'border-border text-text-muted hover:text-text'
                }`}
              >
                格式化
              </button>
              <button
                type="button"
                onClick={() => setRawMode(true)}
                className={`px-2 py-0.5 rounded text-[11px] border transition-colors ${
                  rawMode
                    ? 'border-blue-500/50 bg-blue-500/10 text-blue-600 dark:text-blue-300'
                    : 'border-border text-text-muted hover:text-text'
                }`}
              >
                原始
              </button>
            </div>
          )}
          {rawMode ? (
            <pre className="text-xs text-text whitespace-pre-wrap break-words font-mono">{content}</pre>
          ) : jsonData !== null ? (
            <JsonTreeView data={jsonData} />
          ) : isJsonSection || isPlainTextSection ? (
            <pre className="text-xs text-text whitespace-pre-wrap break-words font-mono">{content}</pre>
          ) : (
            <MarkdownRenderer
              content={content}
              className="text-sm text-text chat-text max-w-none"
            />
          )}
        </div>
      </div>
    </div>
  );
}
