import { isSkillPackageFile } from './skillPackageFile';

export type ChatFileCategory = 'skill' | 'pdf' | 'spreadsheet' | 'presentation' | 'image' | 'document' | 'file';

const CATEGORY_EXTENSIONS: ReadonlyArray<readonly [ChatFileCategory, readonly string[]]> = [
  ['pdf', ['.pdf']],
  ['spreadsheet', ['.xlsx', '.xls', '.csv', '.tsv', '.ods']],
  ['presentation', ['.pptx', '.ppt', '.odp']],
  [
    'image',
    ['.png', '.jpg', '.jpeg', '.tif', '.tiff', '.psd', '.svg', '.gif', '.webp', '.bmp', '.ico', '.jfif'],
  ],
  ['document', ['.docx', '.doc', '.rtf', '.odt', '.txt', '.log', '.md', '.markdown', '.json', '.xml']],
];

const EXTENSION_TO_CATEGORY: Readonly<Record<string, ChatFileCategory>> = Object.freeze(
  Object.fromEntries(CATEGORY_EXTENSIONS.flatMap(([category, extensions]) => extensions.map(extension => [extension, category]))) as Record<
    string,
    ChatFileCategory
  >,
);

export interface ChatFileCategoryLike {
  name?: string;
  path?: string;
  is_skill_package?: boolean;
}

export function resolveChatFileCategory(file: ChatFileCategoryLike): ChatFileCategory {
  if (isSkillPackageFile(file)) return 'skill';
  const candidates = [file.name, file.path].filter(Boolean) as string[];
  for (const candidate of candidates) {
    const base = candidate.replace(/\\/g, '/').split('/').pop()?.toLowerCase() || '';
    const dotIndex = base.lastIndexOf('.');
    if (dotIndex <= 0 || dotIndex === base.length - 1) continue;
    const category = EXTENSION_TO_CATEGORY[base.slice(dotIndex)];
    if (category) return category;
  }
  return 'file';
}
