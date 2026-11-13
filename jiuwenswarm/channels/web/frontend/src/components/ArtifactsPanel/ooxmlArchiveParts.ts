import JSZip from 'jszip';

/**
 * OPC addresses parts with '/', but Windows packagers (.NET Framework
 * ZipFile.CreateFromDirectory, PowerShell 5.1 Compress-Archive) store the
 * platform separator, so `ppt\slides\slide1.xml` never matches a target.
 */
export function normalizeOoxmlPartName(name: string): string {
  return name.replace(/\\/g, '/');
}

function isUnsafeOoxmlPartName(path: string): boolean {
  return path.includes('..') || path.startsWith('/');
}

/** Index entries by conforming part name; a conforming spelling wins a duplicate. */
export function indexOoxmlArchiveParts(entries: Iterable<JSZip.JSZipObject>): Map<string, JSZip.JSZipObject> {
  const parts = new Map<string, JSZip.JSZipObject>();
  for (const entry of entries) {
    if (entry.dir) continue;
    const path = normalizeOoxmlPartName(entry.name);
    if (isUnsafeOoxmlPartName(path)) throw new Error('The archive contains an unsafe path');
    if (!parts.has(path) || path === entry.name) parts.set(path, entry);
  }
  return parts;
}

/**
 * Re-write non-conforming entry names for readers that look parts up themselves
 * (ExcelJS). The original buffer is returned untouched when every entry conforms.
 */
export async function conformOoxmlArchivePartNames(buffer: ArrayBuffer): Promise<ArrayBuffer> {
  const source = await JSZip.loadAsync(buffer);
  const entries = Object.values(source.files).filter(entry => !entry.dir);
  const parts = indexOoxmlArchiveParts(entries);
  if (entries.every(entry => normalizeOoxmlPartName(entry.name) === entry.name)) return buffer;
  const archive = new JSZip();
  for (const [path, entry] of parts) archive.file(path, await entry.async('uint8array'));
  return archive.generateAsync({ type: 'arraybuffer' });
}
