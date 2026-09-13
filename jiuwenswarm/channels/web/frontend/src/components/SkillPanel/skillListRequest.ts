export function buildSkillListParams(sessionId: string, refreshMarketplaces: boolean): Record<string, string | boolean> {
  return {
    session_id: sessionId,
    with_installed: true,
    ...(refreshMarketplaces ? { refresh_marketplaces: true } : {}),
  };
}

export class LatestSkillListRequest {
  private current = 0;

  begin(): number {
    this.current += 1;
    return this.current;
  }

  isLatest(requestId: number): boolean {
    return requestId === this.current;
  }
}

type SkillListFetchState = {
  isActive: boolean;
  activeTab: string;
  previousContext: string | null;
  currentContext: string;
  sessionChanged: boolean;
};

export function shouldFetchSkillList(state: SkillListFetchState): boolean {
  if (!state.isActive) return false;
  return (
    state.previousContext === null ||
    state.previousContext === 'inactive' ||
    state.sessionChanged ||
    (state.activeTab === 'my' && state.previousContext !== state.currentContext)
  );
}
