import { basename, extname, resolve } from "node:path";
import type { FileAttachment } from "./protocol.js";

export const IMAGE_MIME_TYPES: Record<string, string> = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
};

const SUPPORTED_FILE_EXTENSIONS = new Set([
  "ts", "tsx", "js", "jsx", "mjs", "cjs", "css", "scss", "sass", "less", "html", "vue", "svelte",
  "py", "pyi", "pyw",
  "rs", "go", "c", "cpp", "h", "hpp", "java", "kt", "kts", "scala", "swift",
  "json", "yaml", "yml", "toml", "xml", "ini", "cfg", "conf", "env",
  "md", "mdx", "txt", "rst", "adoc",
  "sh", "bash", "zsh", "bat", "cmd", "ps1",
  "sql", "graphql", "gql",
  "tf", "hcl", "dockerfile",
  "proto", "thrift", "asm", "lua", "rb", "php", "dart",
  // 资源管理器拖入常见类型（原白名单过窄会导致解析到路径却仍不生成 @mention）
  "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "odt", "rtf", "epub",
  "zip", "gz", "tgz", "bz2", "7z", "rar",
]);

const AT_MENTION_RE = /(^|[\t ])@(?:"([^"]+)"|([^\s]+))/gm;
const PASTED_PATH_RE =
  /"([^"\r\n]+)"|'([^'\r\n]+)'|(file:\/\/[^\s]+|[A-Za-z]:(?:\\[^\\\r\n]+)+|~\/[^\s]+|\.{1,2}\/[^\s]+|\/[^\s]+)/g;

export type AttachmentKind = "image" | "file";

export type AttachmentCandidate = FileAttachment & {
  resolvedPath: string;
};

export type AttachmentExtractionOptions = {
  cwd?: string;
  homeDir?: string;
  classifyAttachment?: (resolvedPath: string) => AttachmentKind | null;
};

export function normalizeComposerPath(raw: string): string | null {
  const trimmed = raw
    .trim()
    .replace(/^@/, "")
    .replace(/^"(.*)"$/, "$1");
  if (!trimmed) return null;
  if (trimmed.startsWith("file://")) {
    try {
      return decodeURIComponent(trimmed.slice("file://".length));
    } catch {
      return trimmed.slice("file://".length);
    }
  }
  return trimmed;
}

export function expandUserPath(
  path: string,
  cwd: string = process.cwd(),
  homeDir: string | undefined = process.env.HOME,
): string {
  if (path === "~") {
    return homeDir ?? path;
  }
  if (path.startsWith("~/")) {
    return resolve(homeDir ?? "~", path.slice(2));
  }
  return resolve(cwd, path);
}

export function formatAttachmentMention(path: string): string {
  return /\s/.test(path) ? `@"${path}"` : `@${path}`;
}

export function detectAttachmentKind(path: string): AttachmentKind | null {
  const ext = extname(path).toLowerCase();
  if (ext in IMAGE_MIME_TYPES) {
    return "image";
  }
  if (SUPPORTED_FILE_EXTENSIONS.has(ext.replace(".", ""))) {
    return "file";
  }
  return null;
}

export function isImageAttachment(path: string): boolean {
  return detectAttachmentKind(path) === "image";
}

export function isSupportedAttachment(path: string): boolean {
  return detectAttachmentKind(path) !== null;
}

function buildAttachmentCandidate(
  rawPath: string,
  options: AttachmentExtractionOptions = {},
): AttachmentCandidate | null {
  const normalized = normalizeComposerPath(rawPath);
  if (!normalized) return null;
  const resolvedPath = expandUserPath(
    normalized,
    options.cwd,
    options.homeDir,
  );
  const kind = options.classifyAttachment?.(resolvedPath) ?? detectAttachmentKind(resolvedPath);
  if (!kind) return null;
  return {
    type: kind,
    path: resolvedPath,
    resolvedPath,
    filename: basename(resolvedPath),
    mimeType: kind === "image" ? IMAGE_MIME_TYPES[extname(resolvedPath).toLowerCase()] : undefined,
  };
}

export function extractAttachmentsFromText(
  text: string,
  options: AttachmentExtractionOptions = {},
): AttachmentCandidate[] {
  const matches = [...text.matchAll(AT_MENTION_RE)];
  const attachments: AttachmentCandidate[] = [];
  const seen = new Set<string>();

  for (const match of matches) {
    const rawPath = match[2] ?? match[3] ?? "";
    const candidate = buildAttachmentCandidate(rawPath, options);
    if (!candidate) {
      continue;
    }
    if (seen.has(candidate.resolvedPath)) {
      continue;
    }
    seen.add(candidate.resolvedPath);
    attachments.push(candidate);
  }

  return attachments;
}

export function extractFilePathsFromPaste(
  text: string,
  options: Omit<AttachmentExtractionOptions, "classifyAttachment"> = {},
): string[] {
  const matches = [...text.matchAll(PASTED_PATH_RE)];
  const paths: string[] = [];
  const seen = new Set<string>();

  for (const match of matches) {
    const rawPath = match[1] ?? match[2] ?? match[3] ?? "";
    const normalized = normalizeComposerPath(rawPath);
    if (!normalized) {
      continue;
    }
    const resolvedPath = expandUserPath(normalized, options.cwd, options.homeDir);
    if (seen.has(resolvedPath)) {
      continue;
    }
    seen.add(resolvedPath);
    paths.push(resolvedPath);
  }

  return paths;
}

/** Same `@path` token pattern as `AT_MENTION_RE`, for single-line cursor lookups. */
const AT_MENTION_PER_LINE_RE = /(^|[\t ])@(?:"([^"]+)"|([^\s]+))/g;

export function findAttachmentTokenAtCursor(
  line: string,
  col: number,
): { start: number; end: number } | null {
  for (const match of line.matchAll(AT_MENTION_PER_LINE_RE)) {
    const startIdx = match.index ?? 0;
    const endIdx = startIdx + match[0].length;
    if (col > startIdx && col <= endIdx) {
      return { start: startIdx, end: endIdx };
    }
  }
  return null;
}

/**
 * Reconciles image `@mentions` in composer text with a sidecar list (e.g. base64 previews).
 * Drops entries no longer present in text and preserves `content` for paths still referenced.
 */
export function syncComposerImageTokens(
  text: string,
  previous: readonly FileAttachment[],
  isImagePath: (path: string) => boolean,
): { normalizedText: string; attachments: FileAttachment[] } {
  const extracted = extractAttachmentsFromText(text, {
    classifyAttachment: (path) => (isImagePath(path) ? "image" : null),
  });

  const prevByPath = new Map(previous.map((a) => [a.path, a]));
  const attachments: FileAttachment[] = extracted.map(({ resolvedPath: _r, ...rest }) => {
    const prev = prevByPath.get(rest.path);
    if (prev?.content) {
      return { ...rest, content: prev.content };
    }
    return rest;
  });

  return { normalizedText: text, attachments };
}
