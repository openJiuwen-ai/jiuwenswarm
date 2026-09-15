import type {
  ArchivedListResponse,
  ArchivedProject,
  ArchivedSession,
} from './archivedTaskClient';

/**
 * 已归档任务页的纯函数层：统一列表的项目分组、分页结果稳定合并、
 * 归档时间格式化与列表响应规整。不依赖 React，可直接被 node 测试覆盖。
 */

export interface ArchivedTaskGroup {
  /** 稳定分组键：项目 ID；无归属会话共用 UNASSIGNED_GROUP_KEY。 */
  key: string;
  projectId: string;
  /** null 表示未归属项目（项目已不存在或会话本就无项目）。 */
  projectName: string | null;
  /** 已归档项目资源；null 表示该分组只是会话的归属展示（项目未归档或无项目），没有项目级操作。 */
  project: ArchivedProject | null;
  sessions: ArchivedSession[];
  /** 分组内最新的归档时间（Unix 秒），用于统一列表排序。 */
  latestArchivedAt: number;
}

export const UNASSIGNED_GROUP_KEY = '__unassigned__';
export type ArchivedListItemsField = 'projects' | 'sessions';

/**
 * 统一列表分组：已归档会话按 project_name 归组（null/空白归入未归属），
 * 已归档项目挂到同 project_id 的分组作为带操作的项目行；
 * 没有归档会话的项目自成一个分组。分组按组内最新归档时间倒序排列。
 */
export function buildArchivedTaskGroups(
  projects: readonly ArchivedProject[],
  sessions: readonly ArchivedSession[],
): ArchivedTaskGroup[] {
  const groups: ArchivedTaskGroup[] = [];
  const groupByKey = new Map<string, ArchivedTaskGroup>();
  const ensureGroup = (key: string, projectId: string, projectName: string | null): ArchivedTaskGroup => {
    let group = groupByKey.get(key);
    if (!group) {
      group = { key, projectId, projectName, project: null, sessions: [], latestArchivedAt: 0 };
      groupByKey.set(key, group);
      groups.push(group);
    }
    return group;
  };

  for (const session of sessions) {
    const unassigned = !session || session.project_name == null || !session.project_name.trim();
    const key = unassigned ? UNASSIGNED_GROUP_KEY : session.project_id;
    const group = ensureGroup(key, unassigned ? '' : session.project_id, unassigned ? null : session.project_name);
    group.sessions.push(session);
    if (session.archived_at > group.latestArchivedAt) group.latestArchivedAt = session.archived_at;
  }

  for (const project of projects) {
    const group = ensureGroup(project.project_id, project.project_id, project.name);
    group.project = project;
    if (project.archived_at > group.latestArchivedAt) group.latestArchivedAt = project.archived_at;
  }

  groups.sort((left, right) => right.latestArchivedAt - left.latestArchivedAt);
  return groups;
}

/**
 * “加载更多”后合并分页结果：保留已有顺序，追加未出现过的新项。
 * 重复项（事件与响应交错时常见）按 id 去重，保留首次出现的位置。
 */
export function mergeArchivedPageItems<T>(
  existing: readonly T[],
  incoming: readonly T[],
  getId: (item: T) => string,
): T[] {
  const seen = new Set(existing.map(getId));
  const merged = [...existing];
  for (const item of incoming) {
    const id = getId(item);
    if (seen.has(id)) continue;
    seen.add(id);
    merged.push(item);
  }
  return merged;
}

/** 归档时间归一化：约定为 Unix 秒；容错接受已是毫秒的大数值。 */
export function normalizeArchivedAt(value: number | undefined | null): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return null;
  return value < 1e11 ? value * 1000 : value;
}

/** `new Date(archived_at * 1000)` 的本地时间格式化；跨年时补年份。 */
export function formatArchivedAt(archivedAt: number | undefined | null, language: string, now = Date.now()): string {
  const normalized = normalizeArchivedAt(archivedAt);
  if (normalized === null) return '';
  const date = new Date(normalized);
  const sameYear = new Date(now).getFullYear() === date.getFullYear();
  return date.toLocaleString(language || undefined, {
    ...(sameYear ? {} : { year: 'numeric' }),
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** 空标题回退到既有的默认会话标题文案，不展示空白行或内部 ID。 */
export function getArchivedSessionTitle(session: Pick<ArchivedSession, 'title'>, fallback: string): string {
  const title = session?.title?.trim();
  return title ? title : fallback;
}

/**
 * 列表响应规整：后端按资源名返回 projects/sessions，页面统一使用 items。
 * 对应资源数组必须存在，否则视为结构异常并抛错——
 * 归档页不做接口降级，结构错误同样进入失败态而不是伪造空列表。
 * 数组中的 null/undefined 元素一并剔除，避免渲染层访问属性时崩溃。
 */
export function normalizeArchivedListResponse<T>(
  payload: unknown,
  itemsField: ArchivedListItemsField,
): ArchivedListResponse<T> {
  const candidate = (payload ?? {}) as Record<string, unknown>;
  const rawItems = candidate[itemsField];
  if (!Array.isArray(rawItems)) {
    throw new Error(`archived list response is missing the ${itemsField} array`);
  }
  const items = rawItems.filter((item): item is T => item != null);
  return {
    items,
    total: typeof candidate.total === 'number' && candidate.total >= 0 ? candidate.total : items.length,
    limit: typeof candidate.limit === 'number' ? candidate.limit : 0,
    offset: typeof candidate.offset === 'number' ? candidate.offset : 0,
    has_more: candidate.has_more === true,
  };
}
