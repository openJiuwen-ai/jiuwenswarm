/**
 * 前端 → 后端：把组件交互转换为 webui.dispatch 请求（含 in-flight 去重）。
 */

import { webClient } from '../../services/webClient';
import { WebUIActionEvent, WebUIRenderTarget } from './types';

const inFlight = new Set<string>();

function actionKey(event: WebUIActionEvent, target: WebUIRenderTarget, payload: Record<string, unknown>): string {
  return [
    target.session_id,
    target.slot_id,
    target.package_id,
    target.instance_key,
    event,
    String(payload.action_id ?? ''),
    JSON.stringify(payload.args ?? {}),
  ].join('|');
}

/** 派发一次 UI 交互；重复点击在首次请求返回前被忽略。 */
export async function dispatchWebUIAction(
  event: WebUIActionEvent,
  target: WebUIRenderTarget,
  payload: Record<string, unknown> = {},
): Promise<void> {
  const key = actionKey(event, target, payload);
  if (inFlight.has(key)) {
    return;
  }
  inFlight.add(key);
  try {
    await webClient.request('webui.dispatch', {
      event,
      session_id: target.session_id,
      target,
      payload,
    });
  } catch {
    // 交互失败不阻塞界面：后端未装配或事件被拒绝时前端保持当前渲染。
  } finally {
    inFlight.delete(key);
  }
}

