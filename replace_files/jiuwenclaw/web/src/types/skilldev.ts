/**
 * SkillDev 类型定义
 *
 * 对应后端: jiuwenclaw/agentserver/skilldev/schema.py
 */

import type { MediaItem } from './message';

/** 任务阶段 */
export type SkillDevStage =
  | 'init'
  | 'clarify'
  | 'question_clarify'
  | 'generate'
  | 'validate'
  | 'skip_tests_confirm'
  | 'test_design'
  | 'test_run'
  | 'evaluate'
  | 'review'
  | 'improve'
  | 'package'
  | 'desc_optimize_confirm'
  | 'desc_optimize'
  | 'completed'
  | 'error'
  /** Agent 模式任务状态（非 Pipeline stage） */
  | 'active'
  | 'idle'
  | 'pending_interaction'
  | 'cancelled'
  | 'agent';

/** Agent 任务状态 */
export type AgentTaskStatus =
  | 'active'
  | 'idle'
  | 'pending_interaction'
  | 'completed'
  | 'error'
  | 'cancelled';

/** 任务模式 */
export type SkillDevTaskMode = 'create' | 'create_with_resources' | 'modify';

/** SkillDev Todo 项状态 */
export type SkillDevTodoStatus = 'pending' | 'in_progress' | 'completed';

/** Todo 项 */
export interface SkillDevTodo {
  id: string;
  label: string;
  status: SkillDevTodoStatus;
}

/** 产物类型 */
export type ArtifactType = 'skill_package' | 'skill_md' | 'test_result' | 'report';

/** 产物 */
export interface SkillDevArtifact {
  id: string;
  name: string;
  type: ArtifactType;
  size_bytes: number;
  browsable?: boolean;
  downloadable?: boolean;
}

/** 开发计划 */
export interface SkillDevPlan {
  skill_name: string;
  display_name?: string;
  description: string;
  purpose?: string;
  tools?: string[];
  files?: string[];
  reasoning?: string;
  directory_structure?: Record<string, string>;
  key_decisions?: string[];
  test_strategy?: {
    approach?: string;
    test_cases_outline?: string[];
  };
  estimated_complexity?: string;
  intent_capture?: {
    what?: string;
    when?: string;
    output_format?: string;
    testable?: boolean;
  };
}

/** 评测结果 */
export interface SkillDevEvalResult {
  score: number;
  passed: number;
  total: number;
  details: Array<{
    case_id: string;
    passed: boolean;
    score: number;
    feedback?: string;
  }>;
}

/** 评测报告 */
export interface SkillDevReport {
  summary: string;
  strengths: string[];
  weaknesses: string[];
  suggestions: string[];
}

/** 澄清问题选项 */
export interface ClarifyQuestionOption {
  id: string;
  label: string;
}

/** 澄清问题 */
export interface ClarifyQuestion {
  id: string;
  question: string;
  options: ClarifyQuestionOption[];
  allow_custom: boolean;
}

/** 澄清答案 - 后端 plan_stage 期望格式 */
export interface ClarifyAnswer {
  question_id: string;
  answer: string;
}

/** 确认请求 */
export interface ConfirmRequest {
  confirm_type:
    | 'question_clarify'
    | 'plan_confirm'
    | 'review'
    | 'desc_optimize_confirm'
    | 'skip_tests_confirm';
  title: string;
  message: string;
  actions: Array<{
    id: string;
    label: string;
    style?: 'primary' | 'secondary' | 'danger';
  }>;
  data: {
    questions?: ClarifyQuestion[];
    plan?: SkillDevPlan;
    benchmark?: unknown;
    report?: SkillDevReport | string;
    iteration?: number;
    current_description?: string;
    skill_name?: string;
    before?: string;
    after?: string;
  };
}

/** 文件树节点 */
export interface FileTreeNode {
  path: string;
  type: 'file' | 'dir';
  size?: number;
  children?: FileTreeNode[];
}

/** SkillDev 状态 */
export interface SkillDevState {
  task_id: string | null;
  stage: SkillDevStage;
  mode: SkillDevTaskMode;
  iteration: number;
  query: string;
  todos: SkillDevTodo[];
  artifacts: SkillDevArtifact[];
  confirmRequest: ConfirmRequest | null;
  isProcessing: boolean;
  isSuspended: boolean;
  error: string | null;
  // 中间产物
  plan?: SkillDevPlan;
  evalResult?: SkillDevEvalResult;
  report?: SkillDevReport;
  // 文件浏览
  fileTree: FileTreeNode[];
  currentFile: { path: string; content: string } | null;
}

export interface SkillDevSessionSummary {
  task_id: string;
  stage: string;
  updated_at: string;
  created_at: string;
  is_suspended: boolean;
  runner?: 'pipeline' | 'agent';
  status?: AgentTaskStatus;
  status_label?: string;
  title?: string;
  todo_progress?: string;
}

export interface SkillDevRestoreSnapshot {
  task_id: string;
  stage: SkillDevStage;
  mode?: SkillDevTaskMode;
  iteration?: number;
  is_suspended: boolean;
  is_processing: boolean;
  /** 恢复后可传给 skilldev.chat 续跑的原始 query */
  query?: string;
  todos: SkillDevTodo[];
  artifacts: SkillDevArtifact[];
  created_at?: string;
  updated_at?: string;
  error?: string | null;
  pending_confirm?: ConfirmRequest | null;
  runner?: 'pipeline' | 'agent';
  status?: AgentTaskStatus;
}

export interface SkillDevRestoreTimelineItem {
  seq: number;
  timestamp: string;
  source: 'user' | 'assistant' | 'system';
  event_type: string;
  payload: Record<string, unknown>;
}

export interface SkillDevRestoreResult {
  task_id: string;
  snapshot: SkillDevRestoreSnapshot;
  timeline_items: SkillDevRestoreTimelineItem[];
  version: string;
  runner?: 'pipeline' | 'agent';
}

/** SkillDev 事件类型 */
export type SkillDevEventType =
  | 'skilldev.started'
  | 'skilldev.stage_changed'
  | 'skilldev.progress'
  | 'skilldev.agent_thinking'
  | 'skilldev.agent_output'
  | 'skilldev.test_progress'
  | 'skilldev.todos_update'
  | 'skilldev.confirm_request'
  | 'skilldev.confirm_resolved'
  | 'skilldev.artifact_ready'
  | 'skilldev.skill_name_ready'
  | 'skilldev.eval_ready'
  | 'skilldev.validate_result'
  | 'skilldev.desc_opt_ready'
  | 'skilldev.tool_call'
  | 'skilldev.tool_result'
  | 'skilldev.error'
  | 'skilldev.suspended'
  | 'skilldev.completed'
  | 'skilldev.agent_completed';

/** Skill 导入模式（与小艺 message.send importType 对齐） */
export type SkillDevImportType = 'vibeImport' | 'directImport';

/** 开始任务参数 */
export interface StartSkillDevParams {
  query: string;
  session_id?: string;
  /** 恢复后继续同一任务时传入（通常等于 session_id） */
  task_id?: string;
  files?: MediaItem[];
  skill_packages?: MediaItem[];
  /** 用户上传的工具/API 说明文件（与 files 分列，后端写入 workspace/resources/tool_specs/） */
  tool_spec_files?: MediaItem[];
  /** directImport：解压校验后打包或 Agent 修复；默认 vibeImport */
  import_type?: SkillDevImportType;
  /** import_type 的 camelCase 别名，与小艺协议一致 */
  importType?: SkillDevImportType;
  [key: string]: unknown;
}

/** 别名: SkillDevStartParams */
export type SkillDevStartParams = StartSkillDevParams;

/** 响应确认参数 */
export interface RespondSkillDevParams {
  task_id: string;
  session_id?: string;
  action: string;
  plan?: SkillDevPlan;
  feedback?: string;
  answers?: ClarifyAnswer[];
  [key: string]: unknown;
}

/** 别名: SkillDevRespondParams */
export type SkillDevRespondParams = RespondSkillDevParams;

/** 状态查询参数 */
export interface SkillDevStatusParams {
  task_id?: string;
  [key: string]: unknown;
}

/** 下载产物参数 */
export interface SkillDevDownloadParams {
  task_id: string;
  [key: string]: unknown;
}

/** 取消任务参数 */
export interface SkillDevCancelParams {
  task_id: string;
  [key: string]: unknown;
}

/** 文件列表参数 */
export interface SkillDevFileListParams {
  task_id: string;
  [key: string]: unknown;
}

/** 文件读取参数 */
export interface SkillDevFileReadParams {
  task_id: string;
  path: string;
  [key: string]: unknown;
}

/** 导入技能包参数 */
export interface SkillDevParseSkillParams {
  session_id?: string;
  task_id?: string;
  skill_package: MediaItem;
  [key: string]: unknown;
}
