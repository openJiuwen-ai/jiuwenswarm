import { isRecord } from '../protocol/utils.ts';
import { webUIInstanceKey } from '../contracts/normalized.ts';
import type { WebUIRuntime } from '../runtime/types.ts';
import type { PresentationDeclaration } from './hostContext.ts';
import { presentationDeclaration } from './declaration.ts';

export const PRESENTATION_START_EVENT = 'webui.presentation.start';
export const PRESENTATION_RESULT_METHOD = 'webui.presentation.result';

export interface PresentationStartOptions {
  runtime: Pick<WebUIRuntime, 'getInstance'>;
  language(): string;
  desktop(): boolean;
  activate(declaration: PresentationDeclaration): Promise<void>;
  result(params: { request_id: string; code: string }): Promise<unknown>;
}

/** A command contains an identity, never a URL or code to execute. */
export function createPresentationStartHandler(options: PresentationStartOptions) {
  return async (payload: unknown): Promise<void> => {
    if (!isRecord(payload) || Object.keys(payload).sort().join(',') !== 'deadline,request_id,revision,target'
      || typeof payload.request_id !== 'string' || !/^[a-f0-9]{32}$/.test(payload.request_id)
      || !Number.isSafeInteger(payload.revision) || (payload.revision as number) < 1
      || !Number.isSafeInteger(payload.deadline) || !isRecord(payload.target)) return;
    const target = payload.target;
    if (Object.keys(target).sort().join(',') !== 'instance_key,package_id,scope,slot_id'
      || target.scope !== 'application' || target.slot_id !== 'application.presentation'
      || typeof target.package_id !== 'string' || !target.package_id || typeof target.instance_key !== 'string') return;
    const key = webUIInstanceKey({ scope: 'application', sessionId: '', slotId: target.slot_id,
      packageId: target.package_id, instanceKey: target.instance_key });
    const snapshot = options.runtime.getInstance(key);
    let code = 'TARGET_INACTIVE';
    if ((payload.deadline as number) <= Date.now()) code = 'TARGET_INACTIVE';
    else if (!options.desktop()) code = 'DESKTOP_REQUIRED';
    else if (snapshot?.connected && snapshot.state === 'active' && snapshot.revision === payload.revision
      && snapshot.frame?.tree.component === 'ApplicationSurface') {
      try {
        await options.activate(presentationDeclaration(key, snapshot.frame.tree, options.language()));
        code = options.runtime.getInstance(key) === snapshot ? 'STARTED' : 'TARGET_INACTIVE';
      } catch (error) {
        code = error instanceof Error && error.message === 'BUSY' ? 'BUSY' : 'OPEN_FAILED';
      }
    }
    await options.result({ request_id: payload.request_id, code });
  };
}
