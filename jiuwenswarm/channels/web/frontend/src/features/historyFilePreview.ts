export function isHistoryPreviewFile(fileName: string): boolean {
  const lowerName = fileName.toLowerCase();
  return lowerName === 'history.json' || lowerName === 'history.jsonl';
}

export function parseHistoryFileContent(content: string): unknown[] {
  const trimmed = content.trim();
  if (!trimmed) {
    return [];
  }
  if (trimmed.startsWith('[')) {
    const parsed = JSON.parse(trimmed);
    return Array.isArray(parsed) ? parsed : [];
  }
  return trimmed
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}
