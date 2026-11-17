import { webRequest } from '../../services/webClient';
import type { ReaComponentConfigureResult, ReaSetupJob, ReaSetupScan } from './types';

export const reaSetupApi = {
  status: (scope: 'all' | 'base' | 'enhanced' = 'all') =>
    webRequest<ReaSetupScan>('rea.status.get', { scope }, { timeoutMs: 180000 }),
  installBase: () => webRequest<ReaSetupJob>('rea.base.install', {}, { timeoutMs: 30000 }),
  installEnhanced: (components?: string[]) =>
    webRequest<ReaSetupJob>(
      'rea.enhanced.install',
      components ? { components } : {},
      { timeoutMs: 30000 },
    ),
  progress: (jobId: string) =>
    webRequest<ReaSetupJob>('rea.setup.progress', { job_id: jobId }, { timeoutMs: 15000 }),
  cancel: (jobId: string) =>
    webRequest<ReaSetupJob>('rea.setup.cancel', { job_id: jobId }, { timeoutMs: 15000 }),
  configure: (id: string, values: Record<string, string>) =>
    webRequest<ReaComponentConfigureResult>(
      'rea.component.configure',
      { id, values },
      { timeoutMs: 120000 },
    ),
  selectFile: (title: string) =>
    webRequest<{ path: string | null; cancelled: boolean }>(
      'path.select_file',
      { title },
      { timeoutMs: 5 * 60 * 1000 },
    ),
  selectDirectory: (title: string) =>
    webRequest<{ path: string | null; cancelled: boolean }>(
      'path.select_directory',
      { title },
      { timeoutMs: 5 * 60 * 1000 },
    ),
};
