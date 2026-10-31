import { useSessionStore } from '../../../channels/web/frontend/src/stores/sessionStore';

/** 主对话下拉选的模型，和普通对话 chat.send 带的一样；后端按 model_name 给登录模型挂凭据。 */
export function duplexModelParams(searchSessionId: string): Record<string, unknown> {
  const sessionId = searchSessionId.replace(/^task-duplex:/, '');
  if (!sessionId) return {};
  const store = useSessionStore.getState();
  const params = store.getOutgoingModelParams(sessionId);
  const selection = params.model_selection as { type?: string } | undefined;
  const modelName = store.getEffectiveModelName(sessionId);
  return selection?.type === 'model_group' || !modelName ? params : { ...params, model_name: modelName };
}
