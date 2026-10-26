import { useId, type CSSProperties, type ReactNode } from 'react';

export type FileTypeIconKey =
  | 'pdf'
  | 'docx'
  | 'sheet'
  | 'ppt'
  | 'html'
  | 'css'
  | 'json'
  | 'ipynb'
  | 'md'
  | 'text'
  | 'python'
  | 'code'
  | 'config'
  | 'archive'
  | 'audio'
  | 'video'
  | 'image'
  | 'file'
  | 'unknown';

const ICON_COLORS: Record<FileTypeIconKey, { base: string; light: string; fold: string }> = {
  pdf: { base: '#E24B4A', light: '#F06A5F', fold: '#C53A3A' },
  docx: { base: '#2F6FED', light: '#5B8FF5', fold: '#2459C7' },
  md: { base: '#0D9488', light: '#2DD4BF', fold: '#0F766E' },
  text: { base: '#64748B', light: '#94A3B8', fold: '#475569' },
  sheet: { base: '#1FA971', light: '#3FBF88', fold: '#178A5B' },
  ppt: { base: '#D97706', light: '#F59E0B', fold: '#B45309' },
  html: { base: '#E44D26', light: '#F16529', fold: '#C13B1A' },
  css: { base: '#264DE4', light: '#2965F1', fold: '#1C3DB0' },
  json: { base: '#CA8A04', light: '#EAB308', fold: '#A16207' },
  ipynb: { base: '#F37726', light: '#F59A56', fold: '#D45F12' },
  python: { base: '#3776AB', light: '#4B8BBE', fold: '#2A5A8A' },
  code: { base: '#7C3AED', light: '#8B5CF6', fold: '#6D28D9' },
  config: { base: '#0E7490', light: '#06B6D4', fold: '#155E75' },
  archive: { base: '#92400E', light: '#B45309', fold: '#78350F' },
  audio: { base: '#DB2777', light: '#EC4899', fold: '#BE185D' },
  video: { base: '#4F46E5', light: '#6366F1', fold: '#3730A3' },
  image: { base: '#E45CA8', light: '#F07BC0', fold: '#C9448F' },
  file: { base: '#3B82F6', light: '#60A5FA', fold: '#2563EB' },
  unknown: { base: '#9AA3AF', light: '#B0B8C2', fold: '#7F8894' },
};

function DocumentShell({
  typeKey,
  children,
  size,
  gradientId,
}: {
  typeKey: FileTypeIconKey;
  children?: ReactNode;
  size: number;
  gradientId: string;
}) {
  const colors = ICON_COLORS[typeKey];
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 48 48"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
      style={{ display: 'block', flexShrink: 0 } as CSSProperties}
    >
      <defs>
        <linearGradient id={gradientId} x1="24" y1="2" x2="24" y2="46" gradientUnits="userSpaceOnUse">
          <stop stopColor={colors.light} />
          <stop offset="1" stopColor={colors.base} />
        </linearGradient>
      </defs>
      <path
        d="M10 4C8.34315 4 7 5.34315 7 7V41C7 42.6569 8.34315 44 10 44H38C39.6569 44 41 42.6569 41 41V16L31 4H10Z"
        fill={`url(#${gradientId})`}
      />
      <path d="M31 4V12C31 14.2091 32.7909 16 35 16H41L31 4Z" fill={colors.fold} />
      <path d="M31 4L41 16H35C32.7909 16 31 14.2091 31 12V4Z" fill="rgba(255,255,255,0.28)" />
      {children}
    </svg>
  );
}

function GlyphPdf() {
  return (
    <path
      fill="white"
      d="M17.8 31.8c.4-3.8 1.9-7.6 4.4-10.5 2.2-2.5 5-4.1 8.2-3.6 1.7.3 2.9 1.6 2.7 3.2-.3 2.2-2.3 3.4-4.4 4.1-2.6.8-5.3.7-7.9 1.5-2.1.7-3.4 2.2-3 5.3zm4.6-1.8c1.6-1.8 4.4-2.1 6.8-2.7 2-.5 3.5-1.3 3.7-2.8.1-.8-.5-1.4-1.4-1.5-2.1-.3-4.1.8-5.7 2.4-1.8 1.9-3 4.3-3.4 6.8.1-.7.1-1.5 0-2.2z"
    />
  );
}

function GlyphLines() {
  return (
    <>
      <rect x="15" y="20" width="18" height="2.6" rx="1.3" fill="white" />
      <rect x="15" y="25.2" width="14" height="2.6" rx="1.3" fill="white" />
      <rect x="15" y="30.4" width="16" height="2.6" rx="1.3" fill="white" />
    </>
  );
}

function GlyphMd() {
  return (
    <>
      <path
        d="M18 19V33M22.5 19V33"
        stroke="white"
        strokeWidth="2.4"
        strokeLinecap="round"
      />
      <path
        d="M16.5 24.5H24"
        stroke="white"
        strokeWidth="2.4"
        strokeLinecap="round"
      />
      <rect x="27" y="22" width="5.5" height="2.2" rx="1.1" fill="white" />
      <rect x="27" y="27" width="8" height="2.2" rx="1.1" fill="white" />
    </>
  );
}

function GlyphTxt() {
  return (
    <path
      d="M17 20H31M24 20V33"
      stroke="white"
      strokeWidth="2.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  );
}

function GlyphSheet() {
  return (
    <>
      <rect x="15" y="19.5" width="5.8" height="5.8" rx="1.1" fill="white" />
      <rect x="22.6" y="19.5" width="5.8" height="5.8" rx="1.1" fill="white" />
      <rect x="30.2" y="19.5" width="2.4" height="5.8" rx="1" fill="white" fillOpacity="0.9" />
      <rect x="15" y="27.2" width="5.8" height="5.8" rx="1.1" fill="white" />
      <rect x="22.6" y="27.2" width="5.8" height="5.8" rx="1.1" fill="white" />
      <rect x="30.2" y="27.2" width="2.4" height="5.8" rx="1" fill="white" fillOpacity="0.9" />
    </>
  );
}

/** Presentation: slide + triangle play mark */
function GlyphPpt() {
  return (
    <>
      <rect x="14.5" y="19.5" width="19" height="13" rx="2" fill="white" fillOpacity="0.95" />
      <path d="M21.5 23L28 26L21.5 29V23Z" fill="#D97706" />
    </>
  );
}

/** HTML: angle brackets */
function GlyphHtml() {
  return (
    <path
      d="M20 21.5L15.5 26L20 30.5M28 21.5L32.5 26L28 30.5"
      stroke="white"
      strokeWidth="2.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  );
}

/** CSS: hash / braces */
function GlyphCss() {
  return (
    <>
      <path
        d="M19 21V32M24 21V32M17.5 24.5H25.5M17.5 28.5H25.5"
        stroke="white"
        strokeWidth="2.2"
        strokeLinecap="round"
      />
      <path
        d="M29 22.5C30.5 22.5 31.5 23.4 31.5 24.8C31.5 26.8 29.2 27.2 29.2 28.8V30.5"
        stroke="white"
        strokeWidth="2.1"
        strokeLinecap="round"
      />
    </>
  );
}

function GlyphCode() {
  return (
    <path
      d="M20.2 22.2L17 25.2L20.2 28.2M27.8 22.2L31 25.2L27.8 28.2M25.2 20.5L22.8 30"
      stroke="white"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  );
}

/** Python: stylized "Py" */
function GlyphPython() {
  return (
    <text
      x="24"
      y="30.5"
      textAnchor="middle"
      fill="white"
      fontSize="13"
      fontWeight="700"
      fontFamily="Arial, Helvetica, sans-serif"
    >
      Py
    </text>
  );
}

function GlyphConfig() {
  return (
    <>
      <circle cx="24" cy="26" r="6.5" stroke="white" strokeWidth="2.2" fill="none" />
      <path
        d="M24 17.5V19.2M24 32.8V34.5M15.5 26H17.2M30.8 26H32.5M18.2 20.2L19.4 21.4M28.6 30.6L29.8 31.8M29.8 20.2L28.6 21.4M19.4 30.6L18.2 31.8"
        stroke="white"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </>
  );
}

function GlyphArchive() {
  return (
    <>
      <rect x="16" y="18.5" width="16" height="15" rx="1.5" fill="white" fillOpacity="0.95" />
      <rect x="21.5" y="18.5" width="5" height="15" fill="#92400E" fillOpacity="0.85" />
      <rect x="22.2" y="24" width="3.6" height="3.2" rx="0.6" fill="white" />
    </>
  );
}

function GlyphAudio() {
  return (
    <>
      <path
        d="M20 22V30M24 20V32M28 23.5V28.5"
        stroke="white"
        strokeWidth="2.6"
        strokeLinecap="round"
      />
    </>
  );
}

function GlyphVideo() {
  return (
    <>
      <rect x="14.5" y="19.5" width="14" height="13" rx="2" fill="white" fillOpacity="0.95" />
      <path d="M29.5 23L35 26.5L29.5 30V23Z" fill="white" />
    </>
  );
}

function GlyphImage() {
  return (
    <>
      <rect x="14.5" y="19" width="19" height="14" rx="2.2" fill="white" fillOpacity="0.95" />
      <circle cx="19.5" cy="23.5" r="1.8" fill="#E45CA8" />
      <path d="M15.5 30.5L21 26L25 29L28.5 25.5L32.5 30.5H15.5Z" fill="#E45CA8" />
    </>
  );
}

function GlyphQuestion() {
  return (
    <text
      x="24"
      y="31.5"
      textAnchor="middle"
      fill="white"
      fontSize="18"
      fontWeight="700"
      fontFamily="Arial, Helvetica, sans-serif"
    >
      ?
    </text>
  );
}

function glyphFor(typeKey: FileTypeIconKey): ReactNode {
  switch (typeKey) {
    case 'pdf':
      return <GlyphPdf />;
    case 'docx':
      return <GlyphLines />;
    case 'md':
      return <GlyphMd />;
    case 'text':
      return <GlyphTxt />;
    case 'sheet':
      return <GlyphSheet />;
    case 'ppt':
      return <GlyphPpt />;
    case 'html':
      return <GlyphHtml />;
    case 'css':
      return <GlyphCss />;
    case 'json':
      return <GlyphCode />;
    case 'ipynb':
      return <GlyphCode />;
    case 'python':
      return <GlyphPython />;
    case 'code':
      return <GlyphCode />;
    case 'config':
      return <GlyphConfig />;
    case 'archive':
      return <GlyphArchive />;
    case 'audio':
      return <GlyphAudio />;
    case 'video':
      return <GlyphVideo />;
    case 'image':
      return <GlyphImage />;
    case 'unknown':
      return <GlyphQuestion />;
    case 'file':
      return null;
    default:
      return <GlyphQuestion />;
  }
}

const CODE_EXTENSIONS = new Set([
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
]);

const CONFIG_EXTENSIONS = new Set([
  '.xml',
  '.yaml',
  '.yml',
  '.toml',
  '.ini',
  '.cfg',
  '.conf',
  '.env',
]);

const ARCHIVE_EXTENSIONS = new Set([
  '.zip',
  '.rar',
  '.7z',
  '.tar',
  '.gz',
  '.tgz',
  '.bz2',
]);

const AUDIO_EXTENSIONS = new Set([
  '.mp3',
  '.wav',
  '.flac',
  '.aac',
  '.ogg',
  '.m4a',
  '.wma',
]);

const VIDEO_EXTENSIONS = new Set([
  '.mp4',
  '.avi',
  '.mov',
  '.mkv',
  '.webm',
  '.wmv',
  '.flv',
]);

const IMAGE_EXTENSIONS = new Set([
  '.png',
  '.jpg',
  '.jpeg',
  '.gif',
  '.webp',
  '.bmp',
  '.svg',
  '.ico',
  '.jfif',
]);

export function getFileTypeIconKeyFromFilename(filename: string, kind?: string): FileTypeIconKey {
  if (kind === 'image') return 'image';
  const idx = filename.lastIndexOf('.');
  const ext = idx >= 0 ? filename.slice(idx).toLowerCase() : '';

  if (ext === '.pdf') return 'pdf';
  if (ext === '.docx' || ext === '.doc' || ext === '.rtf' || ext === '.odt') return 'docx';
  if (ext === '.xlsx' || ext === '.xls' || ext === '.csv' || ext === '.tsv' || ext === '.ods') {
    return 'sheet';
  }
  if (ext === '.ppt' || ext === '.pptx' || ext === '.odp') return 'ppt';
  if (ext === '.html' || ext === '.htm') return 'html';
  if (ext === '.css') return 'css';
  if (ext === '.json') return 'json';
  if (ext === '.ipynb') return 'ipynb';
  if (ext === '.md' || ext === '.markdown') return 'md';
  if (ext === '.txt' || ext === '.log') return 'text';
  if (ext === '.py') return 'python';
  if (CODE_EXTENSIONS.has(ext)) return 'code';
  if (CONFIG_EXTENSIONS.has(ext)) return 'config';
  if (ARCHIVE_EXTENSIONS.has(ext)) return 'archive';
  if (AUDIO_EXTENSIONS.has(ext)) return 'audio';
  if (VIDEO_EXTENSIONS.has(ext)) return 'video';
  if (IMAGE_EXTENSIONS.has(ext)) return 'image';
  if (!ext) return 'file';
  return 'unknown';
}

export function splitFilenameParts(filename: string): { stem: string; extLabel: string } {
  const idx = filename.lastIndexOf('.');
  if (idx <= 0 || idx === filename.length - 1) {
    return { stem: filename || 'file', extLabel: '' };
  }
  return {
    stem: filename.slice(0, idx),
    extLabel: filename.slice(idx + 1).toLowerCase(),
  };
}

export function FileTypeIcon({
  typeKey,
  size = 32,
}: {
  typeKey: FileTypeIconKey;
  size?: number;
}) {
  const reactId = useId().replace(/:/g, '');
  const key: FileTypeIconKey = ICON_COLORS[typeKey] ? typeKey : 'unknown';
  return (
    <DocumentShell typeKey={key} size={size} gradientId={`fti-${key}-${reactId}`}>
      {glyphFor(key)}
    </DocumentShell>
  );
}
