/**
 * Which document links the workbench may embed.
 *
 * The frame is a privileged surface: it carries the person's platform login and
 * is granted the clipboard. The gateway now persists only platform-origin links,
 * but a row written before that check, or a gateway of another version, can
 * still hand the interface an arbitrary address -- so the interface decides for
 * itself, from the same list. A link that is not on a platform host is offered
 * as an external launch only, never framed.
 */
const EXACT_HOSTS = ['docs.google.com', 'drive.google.com'];
const HOST_SUFFIXES = ['.feishu.cn', '.larksuite.com', '.larkoffice.com'];

export function isEmbeddableDocUrl(url: string | undefined | null): boolean {
  if (!url) return false;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  if (parsed.protocol !== 'https:') return false;
  const host = parsed.hostname.toLowerCase();
  if (EXACT_HOSTS.includes(host)) return true;
  return HOST_SUFFIXES.some((s) => host === s.slice(1) || host.endsWith(s));
}
