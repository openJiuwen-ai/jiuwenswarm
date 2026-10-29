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
};

export type SkillInstallResult = {
  success?: boolean;
  confirmation_required?: boolean;
  cancelled?: boolean;
  detail?: string;
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
  const result = await request(params);
  if (!result.confirmation_required) return result;
  const plan = readSkillPackInstallPlan(result);
  if (!plan) throw new Error('skillPackInstall.invalidPlan');
  const approved = await confirm(plan);
  if (!approved || !plan.can_install || plan.install_plan.some(row => row.action === 'blocked')) {
    return { ...result, success: false, cancelled: true };
  }
  const installed = await request({ ...params, plan_id: plan.plan_id, confirmed: true });
  // A replaced/expired plan requires a fresh visible user action. Never treat
  // the preparation response as a completed installation or retry silently.
  if (installed.confirmation_required) throw new Error('skillPackInstall.changed');
  return installed;
}
