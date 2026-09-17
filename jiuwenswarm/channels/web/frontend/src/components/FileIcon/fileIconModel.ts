export type FileIconType = 'video' | 'image' | 'document' | 'audio' | 'archive' | 'code' | 'html' | 'pdf' | 'ppt' | 'word' | 'xls';

const EXTENSION_GROUPS: ReadonlyArray<readonly [FileIconType, readonly string[]]> = [
  ['pdf', ['.pdf']],
  ['ppt', ['.ppt', '.pptx', '.odp']],
  ['word', ['.doc', '.docx', '.rtf', '.odt']],
  ['xls', ['.xls', '.xlsx', '.csv', '.tsv', '.ods']],
  ['html', ['.html', '.htm']],
  ['image', ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.ico', '.jfif']],
  ['audio', ['.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.wma']],
  ['video', ['.mp4', '.avi', '.mov', '.mkv', '.webm', '.wmv', '.flv']],
  ['archive', ['.zip', '.rar', '.7z', '.tar', '.gz', '.tgz', '.bz2']],
  [
    'code',
    [
      '.js',
      '.jsx',
      '.ts',
      '.tsx',
      '.java',
      '.c',
      '.cpp',
      '.h',
      '.hpp',
      '.go',
      '.rs',
      '.rb',
      '.php',
      '.sql',
      '.sh',
      '.bash',
      '.ps1',
      '.py',
      '.css',
      '.json',
      '.ipynb',
      '.xml',
      '.yaml',
      '.yml',
      '.toml',
      '.ini',
      '.cfg',
      '.conf',
      '.env',
    ],
  ],
  ['document', ['.txt', '.md', '.markdown', '.log']],
];

const COMPOUND_EXTENSION_TO_TYPE: Readonly<Record<string, FileIconType>> = Object.freeze({
  '.tar.gz': 'archive',
  '.tar.bz2': 'archive',
  '.tar.xz': 'archive',
});

const EXTENSION_TO_TYPE: Readonly<Record<string, FileIconType>> = Object.freeze(
  Object.fromEntries(EXTENSION_GROUPS.flatMap(([iconType, extensions]) => extensions.map(extension => [extension, iconType]))) as Record<string, FileIconType>,
);

function basename(fileName: string): string {
  const parts = fileName.trim().split(/[\\/]/);
  return parts[parts.length - 1] ?? '';
}

export function getFileExtensionLabel(fileName: string): string {
  const name = basename(fileName);
  const dotIndex = name.lastIndexOf('.');
  if (dotIndex <= 0 || dotIndex === name.length - 1) return '';
  return name.slice(dotIndex + 1).toLowerCase();
}

/** Resolve a file icon from its filename only. File content and MIME are intentionally not inspected. */
export function resolveFileIconType(fileName: string): FileIconType {
  const normalizedName = basename(fileName).toLowerCase();

  for (const [extension, iconType] of Object.entries(COMPOUND_EXTENSION_TO_TYPE)) {
    if (normalizedName.endsWith(extension)) return iconType;
  }

  const dotIndex = normalizedName.lastIndexOf('.');
  if (dotIndex < 0) return 'document';

  return EXTENSION_TO_TYPE[normalizedName.slice(dotIndex)] ?? 'document';
}
