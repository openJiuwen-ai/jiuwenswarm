import { webRequest } from '../../services/webClient';
import { selectLocalFiles } from '../workspace/localFilePicker';
import { selectProjectDirectory } from '../workspace/projectDirectoryPicker';
import type { ReaComponentConfigureResult, ReaSetupJob, ReaSetupScan } from './types';

function pickerError(
  result: { reason: 'unsupported' | 'cancelled' | 'failed'; message?: string },
): Error {
  return new Error(result.message || result.reason);
}

export const reaSetupApi = {
  status: (
    scope: 'all' | 'base' | 'enhanced' = 'all',
    timeoutMs = 180000,
  ) =>
    webRequest<ReaSetupScan>('rea.status.get', { scope }, { timeoutMs }),
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
  selectFile: async (_title: string) => {
    const result = await selectLocalFiles(false);
    if (result.ok) {
      return { path: result.files[0]?.path ?? null, cancelled: false };
    }
    if (result.reason === 'cancelled') {
      return { path: null, cancelled: true };
    }
    throw pickerError(result);
  },
  selectDirectory: async (_title: string) => {
    const result = await selectProjectDirectory();
    if (result.ok) {
      return { path: result.path, cancelled: false };
    }
    if (result.reason === 'cancelled') {
      return { path: null, cancelled: true };
    }
    throw pickerError(result);
  },
};
