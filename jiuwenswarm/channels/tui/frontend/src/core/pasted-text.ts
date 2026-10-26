const PASTED_TEXT_MARKER_RE = /\[Pasted text #(\d+) \+\d+ lines\]/g;

export const PASTED_TEXT_LINE_THRESHOLD = 4;
export const PASTED_TEXT_CHAR_THRESHOLD = 1000;

export function stripBracketedPasteMarkers(text: string): string {
  return text.split("\x1b[200~").join("").split("\x1b[201~").join("");
}

export function normalizePastedText(text: string): string {
  return text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").replace(/\t/g, "    ");
}

export function countPastedTextLines(text: string): number {
  return text.split("\n").length;
}

export function shouldCollapsePastedText(text: string): boolean {
  return (
    countPastedTextLines(text) > PASTED_TEXT_LINE_THRESHOLD ||
    text.length > PASTED_TEXT_CHAR_THRESHOLD
  );
}

export function formatPastedTextMarker(id: number, text: string): string {
  return `[Pasted text #${id} +${countPastedTextLines(text)} lines]`;
}

export function expandPastedTextMarkers(
  text: string,
  pastedTextById: ReadonlyMap<number, string>,
): string {
  return text.replace(PASTED_TEXT_MARKER_RE, (marker, idText: string) => {
    const pastedText = pastedTextById.get(Number.parseInt(idText, 10));
    return pastedText ?? marker;
  });
}
