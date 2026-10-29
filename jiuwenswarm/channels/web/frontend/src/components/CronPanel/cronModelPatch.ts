import type { ModelSelection } from '../../types';

interface ModelFields {
  modelName: string | null;
  modelSelection: ModelSelection | null;
}

export function buildCronModelPatch(value: ModelFields, initial: ModelFields) {
  const left = value.modelSelection;
  const right = initial.modelSelection;
  const sameSelection = left === right || Boolean(left && right && left.type === right.type && left.id === right.id
    && (left.type !== 'model_group' || (right.type === 'model_group' && left.route_id === right.route_id)));
  if (sameSelection && value.modelName === initial.modelName) return {};
  return left ? { model_selection: left } : { model_selection: null, model_name: value.modelName };
}
