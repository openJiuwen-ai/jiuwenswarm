/**
 * Paragraphs as the model reads them, in ``<doc_state>``'s full text and in ``read_doc_section``: the text, pending
 * tracked changes applied, after markers in square brackets for what Word holds beyond the text. ``[Heading2]`` is
 * the built-in style (a custom style shows its name), ``[1.1.]`` the list number Word draws, ``[tbl:1]`` a table
 * at that depth, ``[page-break]`` a manual page break, ``[has: strike, bold]`` formatting on some or all of the
 * text, ``[img]``, ``[field×2]``, ``[fn]`` and ``[en]`` pictures, fields and note references. A plain Normal
 * paragraph has none.
 */
import type { JsonObject } from './gatewayClient';

/** The plain properties every listing loads for every paragraph. */
export const PARAGRAPH_FIELDS = 'items/text,items/style,items/styleBuiltIn,items/isListItem,items/tableNestingLevel,'
  + 'items/outlineLevel';
/** Paragraphs read back from the host in one round trip. */
const BATCH = 100;

/** A heading's level, 1 to 9, from its built-in heading style or its outline level; undefined for body text. */
export function headingLevel(paragraph: Pick<Word.Paragraph, 'styleBuiltIn' | 'outlineLevel'>): number | undefined {
  const styled = /^Heading([1-9])$/.exec(paragraph.styleBuiltIn);
  if (styled) return Number(styled[1]);
  return paragraph.outlineLevel >= 1 && paragraph.outlineLevel <= 9 ? paragraph.outlineLevel : undefined;
}

/** Whether pending tracked changes make ``text`` differ from the final copy, so the reviewed text is read. */
export async function hasTrackedChanges(context: Word.RequestContext): Promise<boolean> {
  const changes = context.document.body.getTrackedChanges();
  changes.load('items');
  await context.sync();
  return changes.items.length > 0;
}

/**
 * Each of ``paragraphs`` (loaded with ``PARAGRAPH_FIELDS``) as one line: its markers and its final text, and with
 * ``formatting`` its font and paragraph format as JSON after it.
 */
export async function markedLines(context: Word.RequestContext, paragraphs: readonly Word.Paragraph[],
  { formatting = false, reviewed }: { formatting?: boolean; reviewed: boolean }): Promise<string[]> {
  const lines: string[] = [];
  for (let from = 0; from < paragraphs.length; from += BATCH) {
    const batch = paragraphs.slice(from, from + BATCH).map(paragraph => {
      if (formatting) paragraph.load('alignment,firstLineIndent,leftIndent,lineSpacing');
      for (const collection of [paragraph.inlinePictures, paragraph.fields, paragraph.footnotes, paragraph.endnotes]) {
        collection.load('items');
      }
      return {
        paragraph,
        list: paragraph.isListItem ? paragraph.listItemOrNullObject.load('listString') : undefined,
        font: paragraph.font.load(`bold,italic,underline,strikeThrough${formatting ? ',name,size,color' : ''}`),
        reviewed: reviewed ? paragraph.getReviewedText('Current') : undefined,
      };
    });
    await context.sync();
    for (const { paragraph, list, font, reviewed: final } of batch) {
      const markers = markersOf(paragraph, list, font);
      const text = final ? final.value.replace(/\r$/, '') : paragraph.text;
      const format = formatting ? ` ${JSON.stringify(formatOf(paragraph, font))}` : '';
      lines.push(`${markers.map(marker => `[${marker}] `).join('')}${text}${format}`);
    }
  }
  return lines;
}

function markersOf(paragraph: Word.Paragraph, list: Word.ListItem | undefined, font: Word.Font): string[] {
  const markers: string[] = [];
  const style = paragraph.styleBuiltIn === 'Other' ? paragraph.style : paragraph.styleBuiltIn;
  if (style && style !== 'Normal') markers.push(style);
  if (list && !list.isNullObject && list.listString) markers.push(list.listString);
  if (paragraph.tableNestingLevel > 0) markers.push(`tbl:${paragraph.tableNestingLevel}`);
  if (paragraph.text.includes('\f')) markers.push('page-break');
  // A property reads null when only some of the text has it.
  const has = [['strike', font.strikeThrough], ['bold', font.bold], ['italic', font.italic],
    ['underline', font.underline === 'None' ? false : font.underline]].filter(([, value]) => value !== false
    && value !== undefined).map(([name]) => name);
  if (has.length) markers.push(`has: ${has.join(', ')}`);
  const counted = (name: string, count: number) => (count > 1 ? `${name}×${count}` : name);
  const [pictures, fields, footnotes, endnotes] = [paragraph.inlinePictures, paragraph.fields, paragraph.footnotes,
    paragraph.endnotes].map(collection => collection.items.length);
  if (pictures) markers.push(counted('img', pictures));
  if (fields) markers.push(counted('field', fields));
  if (footnotes) markers.push(counted('fn', footnotes));
  if (endnotes) markers.push(counted('en', endnotes));
  return markers;
}

function formatOf(paragraph: Word.Paragraph, font: Word.Font): JsonObject {
  return Object.fromEntries(Object.entries({
    font: font.name, size: font.size, bold: font.bold, italic: font.italic, color: font.color,
    alignment: paragraph.alignment, leftIndent: paragraph.leftIndent || undefined,
    firstLineIndent: paragraph.firstLineIndent || undefined, lineSpacing: paragraph.lineSpacing,
  }).filter(([, value]) => value !== undefined && value !== null));
}
