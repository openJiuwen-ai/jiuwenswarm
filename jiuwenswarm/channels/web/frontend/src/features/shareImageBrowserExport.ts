import { updateBrowserShareImageJob, type ShareImageExportJobStatus } from './shareImageJob';

/** Cancel an unopened renderer without cancelling a job another tab has claimed. */
export async function cancelShareImageJobForClosedTab(
  status: ShareImageExportJobStatus,
  tab: Pick<Window, 'closed'> | undefined,
): Promise<ShareImageExportJobStatus> {
  if (!tab?.closed || status.renderer !== 'browser' || status.state !== 'queued') return status;
  if (!status.client_token) throw new Error('share_export_client_token_missing');
  return updateBrowserShareImageJob(status.job_id, status.client_token, {
    state: 'failed', phase: 'failed', error: 'share_export_tab_closed', expected_state: 'queued',
  });
}

/** Open only after the job has selected client rendering. A blocked popup can
 * be retried from the chat's explicit button without creating another job. */
export function openShareImageExportTab(
  status: ShareImageExportJobStatus,
  locale: string,
): Window | null {
  if (!status.client_token) throw new Error('share_export_client_token_missing');
  const query = new URLSearchParams({ client: '1', job_id: status.job_id, locale });
  const hash = new URLSearchParams({ client_token: status.client_token });
  return window.open(`/share-export-runner?${query}#${hash}`, '_blank');
}
