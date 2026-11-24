import { isEnterprise } from '../edition';
import { appendRuntimeScopeQuery } from './runtimeScope';

/** Native image/link requests cannot carry the runtime routing headers. */
export function uploadFileUrl(path: string): string | undefined {
  if (!isEnterprise()) return `/file-api/raw-file?path=${encodeURIComponent(path)}`;
  const query = new URLSearchParams({ path });
  const match = path.replace(/\\/g, '/').match(/\/agent\/sessions\/([^/]+)\/uploads\/[^/]+$/);
  if (!match || match[1] === '.' || match[1] === '..') return undefined;
  query.set('session_id', match[1]);
  appendRuntimeScopeQuery(query);
  return `/file-api/raw-file?${query}`;
}
