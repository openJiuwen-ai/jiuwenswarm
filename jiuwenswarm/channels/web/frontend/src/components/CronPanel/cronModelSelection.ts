import type { ModelEntry, ModelSelection } from '../../types';

/**
 * 反查稳定选择对应条目的展示名。
 *
 * 按 model_id 唯一定位：目录里允许多条目同名（例如两条 model_name 都是
 * "deepseek-flash" 的不同 api_key 配置），按名字反查无法唯一定位，
 * 按 id 则始终命中选中的那一条。
 */
export function modelNameForSelection(
  models: ModelEntry[],
  selection: ModelSelection | null,
): string | null {
  if (!selection || selection.type !== 'model') return null;
  return models.find((model) => model.model_id === selection.id)?.model_name ?? null;
}

/** create/edit 提交值里与模型相关的字段（见 CronTaskFormValue） */
export interface CronModelFormValue {
  modelName: string | null;
  modelSelection: ModelSelection | null;
}

/**
 * cron create/edit payload 的模型字段。
 *
 * 稳定选择存在时必须随 model_name 一并下发：执行会话优先按 model_id
 * 直接定位模型；仅靠 model_name 的旧链路在目录出现重名模型（多条目同
 * model_name）时，执行侧名字反查报 "ambiguous legacy model name"，
 * 任务创建成功但到点秒败且无任何输出。仅名称选择（登录免费模型无
 * model_id）与旧数据保持只发 model_name 的既有行为。
 */
export function cronModelPayloadFields(value: CronModelFormValue): {
  model_name?: string;
  model_selection?: ModelSelection;
} {
  return {
    ...(value.modelName ? { model_name: value.modelName } : {}),
    ...(value.modelSelection ? { model_selection: value.modelSelection } : {}),
  };
}
