/**
 * Host clipboard write shared by Web UI copy controls. Success feedback stays
 * with each control; this helper only reports whether the host accepted a write.
 */

/**
 * Write text to the host clipboard, preferring the async Clipboard API and
 * falling back to `execCommand('copy')` on hosts (jsdom, insecure contexts)
 * that omit it.
 * @param text - the exact text to place on the clipboard.
 * @returns true only when the host accepted a write.
 */
export async function writeClipboard(text: string): Promise<boolean> {
  // lib.dom types clipboard non-optional, but insecure contexts omit it —
  // that runtime gap is exactly what this guard detects.
  /* oxlint-disable-next-line typescript/no-unnecessary-condition */
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Denied after a native dialog (common in Electron) — try execCommand.
    }
  }
  // Best-effort when the async API is missing or rejected; deprecated but retained.
  /* oxlint-disable typescript/no-deprecated */
  const exec =
    typeof document.execCommand === 'function' ? document.execCommand.bind(document) : undefined;
  if (exec === undefined) return false;
  const el = document.createElement('textarea');
  el.value = text;
  el.setAttribute('readonly', '');
  el.style.position = 'fixed';
  el.style.left = '-9999px';
  document.body.appendChild(el);
  el.select();
  try {
    return exec('copy');
  } catch {
    return false;
  } finally {
    el.remove();
  }
  /* oxlint-enable typescript/no-deprecated */
}
