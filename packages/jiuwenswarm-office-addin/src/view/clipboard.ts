/** Put ``text`` on the clipboard; whether it got there. */
export async function writeClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Some webviews lack or deny the async clipboard; a selected copy still works there.
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.left = '-9999px';
  document.body.append(area);
  area.select();
  try {
    return document.execCommand('copy');
  } finally {
    area.remove();
  }
}
