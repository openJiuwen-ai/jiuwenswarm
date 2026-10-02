export function parseEdgeBookmarkFolderPaths(rows: string[]): string[] {
  return [...new Set(rows.flatMap((row) => row.split(/[,，]/).map((path) => path.trim()).filter(Boolean)))];
}
