import type { ModelEntry, ModelSelection } from '../../types';
import { getModelDisplayName } from '../../features/settings/modules/models/modelPresentation';

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
 * 抽屉「未选模型」时提示行的判定结果（仅展示用，不写 form、不进提交值）。
 * 展示条件（表单未选模型且清单已加载）由调用方 CronTaskDrawer 控制。
 */
export type CronDefaultHint =
  | { kind: 'named'; modelName: string }
  | { kind: 'generic' }
  | { kind: 'empty' };

/**
 * 解析「未选模型」提示行内容。
 *
 * 严格命中（alias 或 model_name 精确匹配 defaultModelName）才展示具体模型，
 * 展示名复用 picker 的 getModelDisplayName（alias 优先），保证提示与菜单里
 * 用户看到的条目能对上——重名目录（多条目同 model_name）只有 alias 能区分
 * 默认命中的是哪一条。不做「首个可选模型」兜底——默认未配置/失配时把目录
 * 第一个模型说成默认，既误导展示，也与触发时后端实际解析的结果无关。
 * 命中失败或未配置默认显示通用文案；目录未加载出任何条目时返回警示态
 * （任务触发时可能失败）。
 */
export function resolveCronDefaultHint(
  chatAvailableModels: ModelEntry[],
  defaultModelName: string | null,
): CronDefaultHint {
  if (chatAvailableModels.length === 0) return { kind: 'empty' };
  if (defaultModelName) {
    const matched = chatAvailableModels.find(
      (model) => model.alias === defaultModelName || model.model_name === defaultModelName,
    );
    if (matched) return { kind: 'named', modelName: getModelDisplayName(matched) };
  }
  return { kind: 'generic' };
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
