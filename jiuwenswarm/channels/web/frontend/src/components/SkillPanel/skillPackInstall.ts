export type SkillPackInstallRow = {
  name: string;
  kind: 'skill' | 'skillpack';
  version: string;
  asset_id?: string;
  action: 'reuse' | 'download' | 'blocked';
  reason?: string;
};

export type SkillPackInstallPlan = {
  plan_id: string;
  skill_type: 'skillpack';
  confirmation_required: true;
  can_install: boolean;
  install_plan: SkillPackInstallRow[];
  refreshed?: boolean;
};

export type SkillInstallResult = {
  success?: boolean;
  confirmation_required?: boolean;
  cancelled?: boolean;
  detail?: string;
  code?: string;
  detail_key?: string;
};

export function readSkillPackInstallPlan(value: unknown): SkillPackInstallPlan | null {
  if (!value || typeof value !== 'object') return null;
  const plan = value as SkillPackInstallPlan;
  if (plan.confirmation_required !== true) return null;
  if (
    plan.skill_type !== 'skillpack' || typeof plan.plan_id !== 'string' || !plan.plan_id ||
    typeof plan.can_install !== 'boolean' || !Array.isArray(plan.install_plan) || !plan.install_plan.length ||
    plan.install_plan.some(row =>
      !row || typeof row.name !== 'string' || !row.name || typeof row.version !== 'string' ||
      !['skill', 'skillpack'].includes(row.kind) || !['reuse', 'download', 'blocked'].includes(row.action)
    )
  ) return null;
  return plan;
}

export async function runSkillPackInstall<T extends SkillInstallResult>(
  request: (params: Record<string, unknown>) => Promise<T>,
  params: Record<string, unknown>,
  confirm: (plan: SkillPackInstallPlan) => Promise<boolean>,
): Promise<T> {
  let result = await request(params);
  if (!result.confirmation_required) return result;
  // Refresh at most once, and always show the replacement plan for a new
  // confirmation. Integrity failures must never trigger an automatic retry.
  for (let attempt = 0; attempt < 2; attempt++) {
    const plan = readSkillPackInstallPlan(result);
    if (!plan) throw new Error('skillPackInstall.invalidPlan');
    const approved = await confirm({ ...plan, refreshed: attempt > 0 });
    if (!approved || !plan.can_install || plan.install_plan.some(row => row.action === 'blocked')) {
      return { ...result, success: false, cancelled: true };
    }
    const installed = await request({ ...params, plan_id: plan.plan_id, confirmed: true });
    if (installed.code === 'SKILLPACK_PLAN_CHANGED' && attempt === 0) {
      const refreshParams: Record<string, unknown> = { ...params, expected_skill_type: 'skillpack' };
      delete refreshParams.plan_id;
      delete refreshParams.confirmed;
      result = await request(refreshParams);
      if (!result.confirmation_required) {
        if (result.success === false) return result;
        throw new Error('skillPackInstall.changed');
      }
      continue;
    }
    if (installed.confirmation_required || installed.code === 'SKILLPACK_PLAN_CHANGED') {
      throw new Error('skillPackInstall.changed');
    }
    return installed;
  }
  throw new Error('skillPackInstall.changed');
}
