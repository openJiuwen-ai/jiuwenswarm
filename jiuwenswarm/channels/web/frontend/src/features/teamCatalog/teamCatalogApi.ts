/**
 * Expert-team catalog helpers for jiuwen Web.
 * Plus-menu source = team.templates.list(catalog_only) — preset/sample expert
 * teams (e.g. 研究洞察团队), not the engine shell jiuwen_team.
 */

import { webRequest } from '../../services/webClient';

export const SWARM_SKILL_FORBIDDEN_WHEN_TEAM_BOUND = 'SWARM_SKILL_FORBIDDEN_WHEN_TEAM_BOUND';
export const SWARM_SKILL_MULTIPLE_NOT_ALLOWED = 'SWARM_SKILL_MULTIPLE_NOT_ALLOWED';

export interface TeamTemplateSummary {
  template_id: string;
  display_name?: string;
  team_name?: string;
  description?: string;
  expose_in_catalog?: boolean;
  source?: string;
}

export interface TeamBindingSummary {
  team_name: string;
  template_id?: string;
  selectable?: boolean;
  disabled_reason?: string;
}

export async function listTeamTemplates(): Promise<TeamTemplateSummary[]> {
  const payload = await webRequest<{ templates?: TeamTemplateSummary[] }>(
    'team.templates.list',
    { catalog_only: true },
    { timeoutMs: 12_000 },
  );
  const templates = Array.isArray(payload?.templates) ? payload.templates : [];
  // Defense in depth: never surface the engine shell in the plus menu.
  return templates.filter((item) => {
    const id = String(item.template_id || '').trim().toLowerCase();
    const name = String(item.team_name || '').trim().toLowerCase();
    return id !== 'jiuwen_team' && id !== 'jiuwen-team'
      && name !== 'jiuwen_team' && name !== 'jiuwen-team';
  });
}

export async function listTeamBindings(): Promise<TeamBindingSummary[]> {
  const payload = await webRequest<{ teams?: TeamBindingSummary[] }>(
    'team.bindings.list',
    {},
    { timeoutMs: 12_000 },
  );
  return Array.isArray(payload?.teams) ? payload.teams : [];
}

export async function ensureBindingAndBindSession(params: {
  sessionId: string;
  templateId: string;
  teamName?: string;
}): Promise<{ team_name: string }> {
  const templateId = params.templateId.trim();
  if (!templateId) {
    throw new Error('template_id is required');
  }
  const preferredName = (params.teamName || templateId).trim();
  let teamName = preferredName;
  try {
    const created = await webRequest<{ team?: { team_name?: string }; team_name?: string }>(
      'team.binding.create',
      {
        template_id: templateId,
        team_name: preferredName,
      },
      { timeoutMs: 20_000 },
    );
    teamName = String(
      created?.team?.team_name || created?.team_name || preferredName,
    ).trim();
  } catch (err) {
    // Binding may already exist for this template id / preferred name — reuse it.
    const code = (err as { code?: string } | null)?.code;
    if (code !== 'CONFLICT') {
      throw err;
    }
    const bindings = await listTeamBindings();
    const existing = bindings.find((item) => {
      const tid = String(item.template_id || '').trim();
      const name = String(item.team_name || '').trim();
      return tid === templateId || name === preferredName;
    });
    const resolved = String(existing?.team_name || '').trim();
    if (!resolved) {
      throw err;
    }
    teamName = resolved;
  }
  if (!teamName) {
    throw new Error('team binding create did not return team_name');
  }
  await webRequest(
    'team.session.bind',
    {
      session_id: params.sessionId,
      team_name: teamName,
      mode: 'team',
    },
    { timeoutMs: 20_000 },
  );
  return { team_name: teamName };
}

/** Runtime dissolve / unbind — kept for non-Web callers; Web UI no longer exposes it. */
export async function dissolveSessionTeam(params: {
  sessionId: string;
  teamName?: string;
}): Promise<void> {
  const body: Record<string, string> = { session_id: params.sessionId };
  if (params.teamName?.trim()) {
    body.team_name = params.teamName.trim();
  }
  await webRequest('team.runtime.dissolve', body, { timeoutMs: 30_000 });
}
