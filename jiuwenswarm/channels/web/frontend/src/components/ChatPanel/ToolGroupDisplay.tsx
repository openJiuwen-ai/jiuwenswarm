import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { ToolExecution } from '../../types';
import { formatToolArguments, formatToolResult } from '../../utils';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import { SkillTreePath } from './SkillTreePath';
import { BeamSearchTree } from './BeamSearchTree';
import { classifyToolCall, describeToolCall, type ToolCategory } from './toolCategory';

interface ToolGroupDisplayProps {
  executions: ToolExecution[];
  notices?: string[];
  showAvatar?: boolean;
  teamLayout?: boolean;
  collapseSkillTreeWhenContentStarts?: boolean;
  viewedSkillIds?: string[];
}

type ToolStatusTone = 'success' | 'warning' | 'error' | 'pending';

function ToolStatusIcon({
  tone,
  className,
}: {
  tone: ToolStatusTone;
  className?: string;
}) {
  return (
    <span className={clsx('tool-status-icon', `is-${tone}`, className)}>
      {tone === 'success' ? (
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
          <circle cx="10" cy="10" r="6.8" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M7.2 10.15 9.1 12.05l3.7-4.05" />
        </svg>
      ) : tone === 'error' ? (
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
          <circle cx="10" cy="10" r="6.8" />
          <path strokeLinecap="round" d="m7.6 7.6 4.8 4.8M12.4 7.6l-4.8 4.8" />
        </svg>
      ) : tone === 'warning' ? (
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
          <circle cx="10" cy="10" r="6.8" />
          <path strokeLinecap="round" d="M10 6.4v4.5" />
          <circle cx="10" cy="13.65" r="0.75" fill="currentColor" stroke="none" />
        </svg>
      ) : (
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
          <circle cx="10" cy="10" r="6.8" opacity="0.4" />
          <circle cx="10" cy="10" r="2.1" fill="currentColor" stroke="none" />
        </svg>
      )}
    </span>
  );
}

export function isToolResultSuccessful(result?: ToolExecution['result']) {
  if (!result) {
    return false;
  }
  if (result.timedOut) {
    return false;
  }
  return Boolean(result.success && !result.result.includes('success=False'));
}

/** 失败与超时统一按失败态展示（文案可区分超时）。 */
export function isToolExecutionFailed(execution: ToolExecution): boolean {
  if (execution.status === 'error' || execution.status === 'timeout') {
    return true;
  }
  if (execution.result && !isToolResultSuccessful(execution.result)) {
    return true;
  }
  return false;
}

function getExecutionLabel(
  execution: ToolExecution,
  sessionCompletedLabel: string,
  t: (key: string) => string
) {
  if (execution.toolCall.name === 'session') {
    return execution.toolCall.formatted_args || sessionCompletedLabel;
  }

  return describeToolCall(execution.toolCall, t);
}

function isSkillToolName(name: string): boolean {
  const normalized = name.trim().toLowerCase();
  const compact = normalized.replace(/[\s-]+/g, '_');
  return (
    compact === 'skill_tool' ||
    compact.endsWith('.skill_tool') ||
    compact.endsWith('/skill_tool') ||
    compact.endsWith(':skill_tool')
  );
}

function addViewedSkillName(out: Set<string>, value: unknown) {
  if (typeof value !== 'string') {
    return;
  }
  const skillName = value.trim();
  if (skillName) {
    out.add(skillName);
  }
}

function addViewedSkillNameFromArgs(out: Set<string>, args: Record<string, unknown> | null | undefined) {
  if (!args) {
    return;
  }
  addViewedSkillName(out, args.skill_name);
  addViewedSkillName(out, args.skillName);
}

function addViewedSkillNameFromText(out: Set<string>, value: string | undefined) {
  const text = String(value || '').trim();
  if (!text) {
    return;
  }

  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      addViewedSkillNameFromArgs(out, parsed as Record<string, unknown>);
      return;
    }
  } catch {
    // formatted_args is often a display string, not JSON.
  }

  const match = text.match(/["']?skill[_-]?name["']?\s*[:=]\s*["']?([^"',}\]\s]+)/i);
  addViewedSkillName(out, match?.[1]);
}

export function collectViewedSkillIds(executions: ToolExecution[]): string[] {
  const out = new Set<string>();
  executions.forEach((execution) => {
    if (!isSkillToolName(execution.toolCall.name)) {
      return;
    }
    addViewedSkillNameFromArgs(out, execution.toolCall.arguments);
    addViewedSkillNameFromText(out, execution.toolCall.formatted_args);
  });
  return Array.from(out);
}

/** 行内下拉展开的工具详情：工具名 + 参数 + 结果（替代原弹窗）。 */
function ToolExecutionDetails({ execution }: { execution: ToolExecution }) {
  const { t } = useTranslation();
  const { toolCall, result, status } = execution;
  const isTimeout = status === 'timeout' || Boolean(result?.timedOut);
  const failed = isToolExecutionFailed(execution);
  const resultSuccess = Boolean(result) && !failed;
  const hasArguments = Object.keys(toolCall.arguments).length > 0;
  const toolNameLabel = toolCall.name?.trim() || result?.toolName || 'tool';

  return (
    <div className="tool-tree-item__detail" data-testid="chat-panel-tool-execution-details">
      <div className="tool-tree-item__detail-block" data-testid="chat-panel-tool-execution-details-name">
        <div className="tool-tree-item__detail-label" data-testid="chat-panel-tool-execution-details-label">
          {t('chatUi.toolResult.toolName')}
        </div>
        <pre className="tool-tree-item__detail-pre tool-tree-item__detail-pre--name">
          {toolNameLabel}
        </pre>
      </div>

      {hasArguments && (
        <div className="tool-tree-item__detail-block" data-testid="chat-panel-tool-execution-details-arguments">
          <div className="tool-tree-item__detail-label">
            {t('chatUi.toolResult.arguments')}
          </div>
          <pre className="tool-tree-item__detail-pre">
            {formatToolArguments(toolCall.arguments)}
          </pre>
        </div>
      )}

      {result && (
        <div className="tool-tree-item__detail-block" data-testid="chat-panel-tool-execution-details-result">
          <div className="tool-tree-item__detail-label">
            {t('chatUi.toolResult.result')}
            {failed && (
              <span
                data-testid="chat-panel-tool-execution-details-badge"
                data-variant={isTimeout ? 'timeout' : 'failed'}
                className={clsx(
                  'tool-tree-item__detail-badge',
                  'is-error',
                  isTimeout && 'is-timeout'
                )}
              >
                {isTimeout ? t('chatUi.toolResult.timeout') : t('chatUi.toolResult.failed')}
              </span>
            )}
            {resultSuccess && (
              <span className="tool-tree-item__detail-badge is-success" data-testid="chat-panel-tool-execution-details-badge" data-variant="success">
                {t('chatUi.toolResult.success')}
              </span>
            )}
          </div>
          {result.skillTree && <SkillTreePath tree={result.skillTree} stepIntervalMs={0} />}
          {(!result.skillTree || result.result) && (
            <pre
              className={clsx(
                'tool-tree-item__detail-pre',
                failed && 'is-failed',
                result.skillTree && 'mt-2'
              )}
            >
              {formatToolResult(result.result)}
            </pre>
          )}
        </div>
      )}

      {!result && isTimeout && (
        <div className="tool-tree-item__detail-status is-error" data-testid="chat-panel-tool-execution-details-status" data-variant="timeout">
          <ToolStatusIcon tone="error" />
          <span>{t('chatUi.toolResult.timeout')}</span>
        </div>
      )}

      {!result && !isTimeout && (
        <div className="tool-tree-item__detail-status is-pending" data-testid="chat-panel-tool-execution-details-status" data-variant="running">
          <ToolStatusIcon tone="pending" />
          <span>{t('chatUi.toolResult.running')}</span>
        </div>
      )}
    </div>
  );
}

/**
 * 是否按「执行中」展示。已完成/失败/超时，或已有结果，一律不当作执行中，
 * 避免 tool_update / 思考整理后重渲染把旧工具误显示成执行中。
 */
function isDisplayRunning(execution: ToolExecution): boolean {
  if (
    execution.status === 'completed' ||
    execution.status === 'error' ||
    execution.status === 'timeout'
  ) {
    return false;
  }
  if (execution.result) {
    return false;
  }
  return execution.status === 'pending';
}

interface GroupHeaderLine {
  key: string;
  category: ToolCategory;
  text: string;
  running: boolean;
  failed: boolean;
  executions: ToolExecution[];
}

/**
 * 每条工具单独一行展示可读动作名（优先后端 display_name），
 * 如「抓取 workbuddy.ai」「写入 DESIGN.md」；不再按分类收成「已完成 N 次…」。
 */
function buildGroupLines(
  executions: ToolExecution[],
  t: (key: string, options?: Record<string, unknown>) => string
): GroupHeaderLine[] {
  const sessionCompletedLabel = t('chatUi.toolGroup.sessionCompleted');
  return executions.map((execution) => {
    const category = classifyToolCall(execution.toolCall.name);
    const running = isDisplayRunning(execution);
    const failed = !running && isToolExecutionFailed(execution);
    const label = getExecutionLabel(execution, sessionCompletedLabel, t);
    return {
      key: execution.toolCallId,
      category,
      running,
      failed,
      executions: [execution],
      text: running
        ? t('chatUi.toolGroup.running', { label })
        : failed
          ? t('chatUi.toolGroup.failed', { label })
          : t('chatUi.toolGroup.completed', { label }),
    };
  });
}

/** 五类任务各自的图标（file/search/code/system/other）。 */
function CategoryIcon({ category }: { category: ToolCategory }) {
  return (
    <span className="tool-tree__cat-icon" aria-hidden="true" data-testid="chat-panel-tool-tree-cat-icon" data-variant={category}>
      {category === 'file' ? (
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M5.5 3.5h5L15 8v8a.9.9 0 0 1-.9.9H5.5a.9.9 0 0 1-.9-.9V4.4a.9.9 0 0 1 .9-.9z" />
          <path d="M10.3 3.5V8H15" />
        </svg>
      ) : category === 'search' ? (
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="9" cy="9" r="4.3" />
          <path d="m12.3 12.3 3.4 3.4" />
        </svg>
      ) : category === 'code' ? (
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="m7.4 6.5-3.4 3.5 3.4 3.5" />
          <path d="m12.6 6.5 3.4 3.5-3.4 3.5" />
        </svg>
      ) : category === 'system' ? (
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3.5" y="4.5" width="13" height="11" rx="1.6" />
          <path d="m6.5 8.6 2.3 1.9-2.3 1.9" />
          <path d="M10.8 12.7h3" />
        </svg>
      ) : (
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M13.4 4.6a2.7 2.7 0 0 0-3.3 3.4l-5 5a1.3 1.3 0 1 0 1.9 1.9l5-5a2.7 2.7 0 0 0 3.4-3.3l-2 2-1.9-.1-.1-1.9 2-2z" />
        </svg>
      )}
    </span>
  );
}

export { formatDurationPrecise, useNow } from './chatTimelineClock';

export function ToolGroupDisplay({
  executions,
  notices = [],
  showAvatar = true,
  teamLayout = false,
  collapseSkillTreeWhenContentStarts = false,
  viewedSkillIds: turnViewedSkillIds = [],
}: ToolGroupDisplayProps) {
  const { t } = useTranslation();
  const [openKeys, setOpenKeys] = useState<Record<string, boolean>>({});
  const toggleLine = useCallback((key: string) => {
    setOpenKeys((current) => ({ ...current, [key]: !current[key] }));
  }, []);
  const visibleExecutions = teamLayout
    ? executions.filter((execution) => !execution.toolCall.memberName)
    : executions;

  const headerLines = buildGroupLines(visibleExecutions, t);
  const skillTreeExecutions = visibleExecutions.filter(
    (execution) => execution.result?.skillTree
  );
  const skillTrees = skillTreeExecutions
    .map((execution) => execution.result?.skillTree)
    .filter((tree): tree is NonNullable<typeof tree> => Boolean(tree));
  const beamSearch = [...visibleExecutions]
    .reverse()
    .find((execution) => execution.result?.beamSearch)
    ?.result?.beamSearch;
  const viewedSkillIds = Array.from(new Set([
    ...turnViewedSkillIds,
    ...collectViewedSkillIds(executions),
  ]));
  if (visibleExecutions.length === 0) {
    return null;
  }

  return (
    <div
      className={clsx(
        'tool-group-frame',
        teamLayout && 'tool-group-frame--team'
      )}
      data-testid="chat-panel-tool-group"
    >
      <div className="pt-0.5 tool-group-frame__avatar" data-testid="chat-panel-tool-group-avatar">
        {showAvatar ? (
          <TeamMemberAvatar member="team_leader" />
        ) : null}
      </div>
      <div className="min-w-0">
        <div className="tool-tree" data-testid="chat-panel-tool-tree">
          {notices.length > 0 && (
            <div className="tool-tree__notices" data-testid="chat-panel-tool-tree-notices">
              {notices.map((notice) => (
                <div key={notice} className="tool-tree__notice" data-testid="chat-panel-tool-tree-notice" data-variant={notice}>
                  {notice}
                </div>
              ))}
            </div>
          )}
          {headerLines.map((line) => {
            const open = Boolean(openKeys[line.key]);
            return (
              <div key={line.key} className="tool-tree__section" data-testid="chat-panel-tool-tree-section" data-variant={line.key}>
                <button
                  type="button"
                  className="tool-tree__header"
                  onClick={() => toggleLine(line.key)}
                  aria-expanded={open}
                  data-testid="chat-panel-tool-tree-header"
                >
                  <span className="tool-tree__header-line" data-testid="chat-panel-tool-tree-header-line">
                    <CategoryIcon category={line.category} />
                    <span
                      className={clsx(
                        'tool-tree__header-line-text',
                        line.running && 'is-running',
                        line.failed && 'is-failed'
                      )}
                      data-testid="chat-panel-tool-tree-header-line-text"
                      data-variant={line.running ? 'running' : line.failed ? 'failed' : 'completed'}
                    >
                      {line.text}
                    </span>
                    <span
                      className={clsx('tool-tree-item__disclosure', open && 'is-open')}
                      aria-hidden="true"
                    >
                      <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
                        <path strokeLinecap="round" strokeLinejoin="round" d="m8 6 4 4-4 4" />
                      </svg>
                    </span>
                  </span>
                </button>

                <div className={clsx('tool-tree-item__collapse', open && 'is-open')} data-testid="chat-panel-tool-tree-item-collapse">
                  <div className="tool-tree-item__collapse-inner">
                    {line.executions[0] ? (
                      <div className="tool-tree-item__detail-wrap">
                        <ToolExecutionDetails execution={line.executions[0]} />
                      </div>
                    ) : null}
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        {skillTrees.length > 0 && (
          <SkillTreePath
            trees={skillTrees}
            viewedSkillIds={viewedSkillIds}
            autoCollapse={collapseSkillTreeWhenContentStarts}
          />
        )}
        {beamSearch && (
          <BeamSearchTree
            progress={beamSearch}
            autoCollapse={collapseSkillTreeWhenContentStarts}
          />
        )}
      </div>
    </div>
  );
}
