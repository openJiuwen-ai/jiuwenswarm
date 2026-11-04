import * as readiness from './designModelsReadiness';

// The model policy owns protocol restrictions. Some editions only provide the
// async readiness check and have no synchronous restriction on a loaded model.
const policy: {
  findDesignModelsProblem(sessionId?: string | null): Promise<string | null>;
  loadedDesignChatAnthropicProblem?(sessionId?: string | null, modelName?: string | null): string | null;
} = readiness;

export const findDesignModelsProblem = policy.findDesignModelsProblem;

export function loadedDesignChatAnthropicProblem(sessionId?: string | null, modelName?: string | null): string | null {
  return policy.loadedDesignChatAnthropicProblem?.(sessionId, modelName) ?? null;
}
