/**
 * What Word code may not do, whatever the user's approval settings: change what every document on the computer
 * shares (the Normal template and the other templates, their building blocks, the list gallery, the master list of
 * sources), open or create other documents, or bring in content that Word fetches from another file, program or
 * computer. The industry add-in asks the user in its pane for these; the approval here is the backend's, before
 * the call, so the pane refuses them outright. The runner already keeps the network APIs out of reach.
 */
import type { HostWrite } from './officeCodeRunner';

/** Objects whose changes reach every document on this computer. */
const SHARED = new Set(['templates', 'attachedTemplate', 'listTemplateGalleries', 'bibliography',
  'buildingBlockEntries', 'buildingBlockTypes']);
/** Field types whose result Word fetches from another file, program or address. */
const FETCHING_FIELDS = /^(?:IncludeText|IncludePicture|Link|Dde|DdeAuto|Import)$/i;
/**
 * The same fields, a linked picture (``r:link``) or any part outside the package (``TargetMode="External"``) in
 * inserted OOXML, or an outside picture source in HTML.
 */
const FETCHING_MARKUP = new RegExp(String.raw`(?:w:instr\s*=\s*["']|<w:instrText\b[^>]*>)\s*`
  + String.raw`(?:INCLUDETEXT|INCLUDEPICTURE|LINK|DDE|DDEAUTO|IMPORT)\b|\br:link\s*=|\bTargetMode\s*=\s*["']?`
  + String.raw`\s*External|\bsrc\s*=\s*["']?\s*(?:https?:|ftp:|file:|\\\\|\/\/)`, 'i');

const SHARED_REASON = 'changes what every document on this computer shares (the Normal template, building blocks, '
  + 'the list gallery or the master list of sources)';

export function screenWordWrite({ path, name, values }: HostWrite): string | undefined {
  if ([...path, name].some(step => SHARED.has(step))) return `The code ${SHARED_REASON}.`;
  if (name === 'openDocument' || name === 'createDocument') {
    return 'Code here works on this document only; it cannot open or create other documents.';
  }
  if (name === 'insertFileFromBase64') {
    return 'Inserting another document can bring in its fields and parts that Word fetches from another file or '
      + 'address.';
  }
  if (name === 'insertField' && values.some(value => typeof value === 'string' && FETCHING_FIELDS.test(value))) {
    return 'The field would make Word fetch content from another file, program or address.';
  }
  const markup = /^insert(?:Ooxml|Html)$/.test(name);
  if (markup && values.some(value => typeof value === 'string' && FETCHING_MARKUP.test(value))) {
    return 'The inserted content would make Word fetch from another file, program or address.';
  }
  return undefined;
}
