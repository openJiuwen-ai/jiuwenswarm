import { component, integer, nil, object, text, token, union } from './shared.ts';
import { validWorkspacePath } from './fileTree.ts';

export const filePreviewRule = component('FilePreview', {
  file: union(nil, object({ name: text, path: text, mimeType: text, size: integer(), content: text }, ['name', 'path', 'content'])),
  emptyText: text, status: token('ready', 'missing', 'unsupported', 'too_large', 'forbidden', 'unavailable', 'error'),
}, {
  required: ['file'],
  validate(p) {
    const file = p.file as { name: string; path: string } | null;
    return file && (!file.name.trim() || !validWorkspacePath(file.path)) ? 'invalid preview file' : null;
  },
});
