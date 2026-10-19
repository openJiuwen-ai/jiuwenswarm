export type GraphActionResponse = {
  success?: boolean;
  detail?: string;
  build_status?: 'idle' | 'running' | 'success' | 'error' | 'cancelled';
};

export type SymphonyGraphPanelHandle = {
  startIncrementalBuild: () => Promise<void>;
  cancelActiveBuild: () => Promise<void>;
};

type GraphActionRequest = (
  method: string,
  params: Record<string, unknown>,
  options: { timeoutMs: number },
) => Promise<GraphActionResponse>;

export type SymphonyEnabledChangeResult = {
  appliedWithoutRestart: boolean;
  configSaveFailed: boolean;
  graphActionError?: string;
};

type CoordinateSymphonyEnabledChangeInput = {
  enabled: boolean;
  save: (enabled: boolean) => Promise<boolean>;
  getGraphPanel: () => SymphonyGraphPanelHandle | null;
  request: GraphActionRequest;
  refreshFailedMessage: string;
  cancelFailedMessage: string;
  onGraphActionStart?: () => void;
};

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export async function coordinateSymphonyEnabledChange({
  enabled,
  save,
  getGraphPanel,
  request,
  refreshFailedMessage,
  cancelFailedMessage,
  onGraphActionStart,
}: CoordinateSymphonyEnabledChangeInput): Promise<SymphonyEnabledChangeResult> {
  let appliedWithoutRestart: boolean;
  try {
    appliedWithoutRestart = await save(enabled);
  } catch {
    return { appliedWithoutRestart: false, configSaveFailed: true };
  }

  if (!appliedWithoutRestart) {
    return { appliedWithoutRestart: false, configSaveFailed: false };
  }

  onGraphActionStart?.();
  try {
    const graphPanel = getGraphPanel();
    if (graphPanel) {
      if (enabled) {
        await graphPanel.startIncrementalBuild();
      } else {
        await graphPanel.cancelActiveBuild();
      }
      return { appliedWithoutRestart: true, configSaveFailed: false };
    }

    if (enabled) {
      const data = await request('skills.graph.build', { force: false }, { timeoutMs: 60_000 });
      if (!data.success) {
        throw new Error(data.detail || refreshFailedMessage);
      }
      return { appliedWithoutRestart: true, configSaveFailed: false };
    }

    const data = await request('skills.graph.cancel', {}, { timeoutMs: 60_000 });
    if (data.build_status !== 'idle' && !data.success) {
      throw new Error(data.detail || cancelFailedMessage);
    }
    return { appliedWithoutRestart: true, configSaveFailed: false };
  } catch (error) {
    return {
      appliedWithoutRestart: true,
      configSaveFailed: false,
      graphActionError: errorMessage(error),
    };
  }
}
