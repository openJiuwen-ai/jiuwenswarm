import { component, integer, nil, object, text, token, union, validURL } from './shared.ts';
import { validWorkspacePath } from './fileTree.ts';

export const filePreviewRule = component('FilePreview', {
  file: union(nil, object({
    name: text, path: text, mimeType: text, size: integer(), content: text, downloadUrl: text,
  }, ['name', 'path'])),
  emptyText: text, status: token('ready', 'missing', 'unsupported', 'too_large', 'forbidden', 'unavailable', 'error'),
}, {
  required: ['file'],
  validate(p) {
    const file = p.file as { name: string; path: string; content?: string; downloadUrl?: string } | null;
    if (!file) return null;
    if (!file.name.trim() || !validWorkspacePath(file.path)) return 'invalid preview file';
    if (file.content === undefined && file.downloadUrl === undefined) return 'preview content or URL is required';
    return file.downloadUrl !== undefined && !validURL(file.downloadUrl) ? 'invalid preview URL' : null;
  },
});
