import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import { create } from 'zustand';
import { Button, Dialog } from '../ui';
import { webRequest } from '../../services/webClient';
import i18n from '../../i18n';
import type { WebRequestOptions } from '../../types';
import {
  runSkillPackInstall,
  type SkillInstallResult,
  type SkillPackInstallPlan,
} from './skillPackInstall';

type PendingPlan = {
  id: number;
  plan: SkillPackInstallPlan;
  finish: (approved: boolean) => void;
};

const useInstallPlans = create<{ pending: PendingPlan[] }>(() => ({ pending: [] }));
let requestSequence = 0;

function confirmInstallPlan(plan: SkillPackInstallPlan, signal?: AbortSignal): Promise<boolean> {
  if (signal?.aborted) return Promise.resolve(false);
  return new Promise(resolve => {
    const id = ++requestSequence;
    const finish = (approved: boolean) => {
      signal?.removeEventListener('abort', cancel);
      useInstallPlans.setState(state => ({ pending: state.pending.filter(item => item.id !== id) }));
      resolve(approved);
    };
    const cancel = () => finish(false);
    signal?.addEventListener('abort', cancel, { once: true });
    useInstallPlans.setState(state => ({ pending: [...state.pending, { id, plan, finish }] }));
  });
}

/** Shared Hub install contract for marketplace and expert-editor consumers. */
export async function installSkillWithPlan<T extends SkillInstallResult>(
  method: string,
  params: Record<string, unknown>,
  options?: WebRequestOptions,
  signal?: AbortSignal,
): Promise<T> {
  try {
    return await runSkillPackInstall<T>(
      next => webRequest<T>(method, next, options),
      params,
      plan => confirmInstallPlan(plan, signal),
    );
  } catch (error) {
    if (error instanceof Error && error.message.startsWith('skillPackInstall.')) {
      throw new Error(i18n.t(error.message));
    }
    throw error;
  }
}

export function SkillPackInstallPlanHost() {
  const pending = useInstallPlans(state => state.pending[0]);
  return pending ? <SkillPackInstallPlanDialog plan={pending.plan} onResolve={pending.finish} /> : null;
}

export function SkillPackInstallPlanDialog({ plan, onResolve: finish }: {
  plan: SkillPackInstallPlan;
  onResolve: (approved: boolean) => void;
}) {
  const { t } = useTranslation();
  const titleId = useId();
  const blocked = !plan.can_install || plan.install_plan.some(row => row.action === 'blocked');
  return (
    <Dialog open titleId={titleId} onCancel={() => finish(false)} onBackdropClick={() => finish(false)}>
      <section className="p-6" data-testid="skill-panel-install-plan">
        <h2 id={titleId} className="text-lg font-semibold text-text" data-testid="skill-panel-install-plan-title">
          {t('skillPackInstall.title')}
        </h2>
        <p className="mt-2 text-sm text-text-muted" data-testid="skill-panel-install-plan-summary">
          {t('skillPackInstall.summary')}
        </p>
        <div className="my-5 max-h-[55vh] overflow-auto rounded-lg border border-border" data-testid="skill-panel-install-plan-members">
          {plan.install_plan.map(row => (
            <div key={`${row.kind}:${row.name}`} className="flex items-start justify-between gap-4 border-b border-border p-3 last:border-b-0" data-testid="skill-panel-install-plan-member" data-variant={row.name}>
              <div className="min-w-0">
                <div className="break-words font-medium text-text">{row.name}</div>
                <div className="mt-1 text-xs text-text-muted">{row.kind === 'skillpack' ? 'SkillPack' : 'Skill'} · {row.version}</div>
                {row.reason && <p className="mt-1 text-xs text-text-muted">{row.reason}</p>}
              </div>
              <span className={`shrink-0 rounded px-2 py-1 text-xs ${row.action === 'blocked' ? 'text-warn' : 'text-text-muted'}`} data-testid="skill-panel-install-plan-action" data-variant={row.action}>
                {t(`skillPackInstall.actions.${row.action}`)}
              </span>
            </div>
          ))}
        </div>
        {blocked && <p className="mb-4 text-sm text-warn" data-testid="skill-panel-install-plan-blocked">{t('skillPackInstall.blocked')}</p>}
        <div className="flex justify-end gap-3">
          <Button variant="secondary" onClick={() => finish(false)} data-testid="skill-panel-install-plan-cancel">
            {t('skillPackInstall.cancel')}
          </Button>
          <Button variant="primary" disabled={blocked} onClick={() => finish(true)} data-testid="skill-panel-install-plan-confirm">
            {t('skillPackInstall.confirm')}
          </Button>
        </div>
      </section>
    </Dialog>
  );
}
