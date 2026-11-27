/**
 * SkillDev 服务
 *
 * 与后端 SkillDevAgent 交互
 * 方法: skilldev.chat, skilldev.respond, skilldev.parse_skill, skilldev.status,
 *      skilldev.download, skilldev.file.list, skilldev.file.read, skilldev.file.write
 */

import { webRequest, webSendStream } from './webClient';
import type {
  StartSkillDevParams,
  RespondSkillDevParams,
  SkillDevPlan,
  FileTreeNode,
  SkillDevSessionSummary,
  SkillDevRestoreResult,
} from '../types/skilldev';
import type { MediaItem } from '../types';

/**
 * 发起新任务（流式，结果通过 skilldev.* 事件推送）
 */
export async function startSkillDev(
  params: StartSkillDevParams
): Promise<void> {
  const importType = params.import_type ?? params.importType;
  await webSendStream(
    'skilldev.chat',
    {
      query: params.query,
      session_id: params.session_id,
      task_id: params.task_id,
      files: params.files,
      skill_packages: params.skill_packages,
      tool_spec_files: params.tool_spec_files,
      ...(importType ? { import_type: importType, importType } : {}),
    }
  );
}

/**
 * 统一确认入口（流式，结果通过 skilldev.* 事件推送）
 */
export async function respondSkillDev(
  params: RespondSkillDevParams
): Promise<void> {
  await webSendStream(
    'skilldev.respond',
    {
      task_id: params.task_id,
      session_id: params.session_id,
      action: params.action,
      plan: params.plan,
      feedback: params.feedback,
      answers: params.answers,
    }
  );
}

/**
 * 导入本地 skill 压缩包（非流式）
 */
export async function parseSkillPackage(
  sessionId: string,
  file: MediaItem,
  taskId?: string
): Promise<{ ok: boolean; task_id: string; message?: string }> {
  console.info('[SkillDevService] send parse skill request', {
    sessionId,
    taskId,
    filename: file?.filename,
    mimeType: file?.mimeType,
    hasBase64Data: Boolean(file?.base64Data),
  });
  const result = await webRequest<{ ok: boolean; task_id: string; message?: string }>(
    'skilldev.parse_skill',
    {
      session_id: sessionId,
      task_id: taskId,
      skill_package: file,
    }
  );
  console.info('[SkillDevService] parse skill response received', {
    ok: result?.ok,
    task_id: result?.task_id,
    message: result?.message,
  });
  return result;
}

/**
 * 查询任务状态
 */
export async function getSkillDevStatus(taskId: string): Promise<{
  task_id: string;
  stage: string;
  mode: string;
  iteration: number;
  plan?: SkillDevPlan;
  eval_results?: unknown;
  created_at: string;
  updated_at: string;
}> {
  const result = await webRequest<{
    task_id: string;
    stage: string;
    mode: string;
    iteration: number;
    plan?: SkillDevPlan;
    eval_results?: unknown;
    created_at: string;
    updated_at: string;
  }>('skilldev.status', { task_id: taskId });

  return result;
}

/**
 * 列出所有任务
 */
export async function listSkillDevTasks(): Promise<string[]> {
  const result = await webRequest<{ tasks: string[] }>(
    'skilldev.status',
    {}
  );

  return result.tasks;
}

/**
 * 获取 SkillDev 会话列表（用于会话标签页）
 */
export async function listSkillDevSessions(): Promise<SkillDevSessionSummary[]> {
  const result = await webRequest<{ sessions: SkillDevSessionSummary[] }>(
    'skilldev.session.list',
    {}
  );
  return Array.isArray(result.sessions) ? result.sessions : [];
}

/**
 * 恢复 SkillDev 会话
 */
export async function restoreSkillDevSession(taskId: string): Promise<SkillDevRestoreResult> {
  return await webRequest<SkillDevRestoreResult>('skilldev.restore', { task_id: taskId });
}

/**
 * 下载产物
 */
export async function downloadSkillDevArtifact(taskId: string): Promise<{
  filename: string;
  content_base64: string;
  size_bytes: number;
}> {
  const result = await webRequest<{
    filename: string;
    content_base64: string;
    size_bytes: number;
  }>('skilldev.download', { task_id: taskId });

  return result;
}

/**
 * 获取文件树
 */
export async function getSkillDevFileTree(taskId: string): Promise<FileTreeNode[]> {
  const result = await webRequest<{ tree: FileTreeNode[] }>(
    'skilldev.file.list',
    { task_id: taskId }
  );

  return result.tree;
}

/**
 * 读取文件内容
 */
export async function readSkillDevFile(
  taskId: string,
  path: string
): Promise<{ path: string; content: string }> {
  const result = await webRequest<{ path: string; content: string }>(
    'skilldev.file.read',
    { task_id: taskId, path }
  );

  return result;
}

/**
 * 修改工作区文件内容
 */
export async function writeSkillDevFile(
  taskId: string,
  path: string,
  content: string
): Promise<{ path: string; size: number; repackaged: boolean }> {
  const result = await webRequest<{ path: string; size: number; repackaged: boolean }>(
    'skilldev.file.write',
    { task_id: taskId, path, content }
  );

  return result;
}

/**
 * 取消任务（skilldev.cancel → gateway → interface._process_interrupt → skilldev adapter）
 */
export async function cancelSkillDev(params: {
  session_id: string;
  task_id?: string;
}): Promise<void> {
  const sessionId = params.session_id;
  const taskId = params.task_id ?? sessionId;
  await webRequest<{ accepted?: boolean }>('skilldev.cancel', {
    session_id: sessionId,
    task_id: taskId,
  });
}

/**
 * 触发下载
 */
export function triggerDownload(filename: string, contentBase64: string): void {
  const byteCharacters = atob(contentBase64);
  const byteNumbers = new Array(byteCharacters.length);
  for (let i = 0; i < byteCharacters.length; i++) {
    byteNumbers[i] = byteCharacters.charCodeAt(i);
  }
  const byteArray = new Uint8Array(byteNumbers);
  const blob = new Blob([byteArray]);

  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
