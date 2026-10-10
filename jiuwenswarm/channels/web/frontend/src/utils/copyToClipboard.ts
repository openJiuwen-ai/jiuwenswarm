/**
 * Copy text to the system clipboard, reporting whether the host accepted it.
 *
 * `navigator.clipboard.writeText` is tried first; when it rejects (blocked
 * permission, insecure context, embedded iframe) a hidden textarea plus
 * `document.execCommand('copy')` is the fallback. Unlike a bare `writeText`
 * await, the boolean return lets callers show a failure state instead of a
 * false "Copied" — `execCommand` itself must be honored, and it can fail too.
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    let copied = false;
    try {
      copied = document.execCommand('copy');
    } catch {
      copied = false;
    }
    document.body.removeChild(textarea);
    return copied;
  }
}
