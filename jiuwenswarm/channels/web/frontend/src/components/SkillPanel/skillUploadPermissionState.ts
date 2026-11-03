import type { SkillUploadPermissionState } from '../../services/skillUploadPermission';

export type UploadPermissionPhase = 'idle' | 'loading' | 'error';

/** 企业版上传入口应呈现的形态。 */
export type UploadEntry = 'checking' | 'upload' | 'apply' | 'pending' | 'unavailable';

export function resolveUploadEntry(
  state: SkillUploadPermissionState | null,
  phase: UploadPermissionPhase,
): UploadEntry {
  if (phase === 'error') return 'unavailable';
  if (phase === 'loading' && !state) return 'checking';
  if (state?.allowed) return 'upload';
  if (state?.application_status === 'pending') return 'pending';
  return 'apply';
}

/** 入口按钮文案键：只有明确允许时才显示"上传 Skill"。 */
export function uploadEntryAction(entry: UploadEntry): 'upload' | 'apply' {
  return entry === 'upload' ? 'upload' : 'apply';
}
