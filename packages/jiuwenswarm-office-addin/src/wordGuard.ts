/**
 * What Word code may not do, whatever the user's approval settings: change what every document on the computer
 * shares (the Normal template and the other templates, their building blocks, the list gallery, the master list of
 * sources), open or create other documents, or bring in content that Word fetches from another file, program or
 * computer. The industry add-in asks the user in its pane for these; the approval here is the backend's, before
 * the call, so the pane refuses them outright. The runner already keeps the network APIs out of reach. On WPS
 * Writer the same holds for ``execute_wps_js`` over the VBA-style object model, which also keeps the application,
 * files and hyperlinks out of reach (``WPS_WRITER_UNREACHABLE``).
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

/**
 * What ``execute_wps_js`` code may not reach from a document, lower-cased: the application and the way back to it
 * (``Parent``), the templates every document shares, other files and programs, hyperlinks and mail, and saving,
 * printing or closing.
 */
export const WPS_WRITER_UNREACHABLE: ReadonlySet<string> = new Set(['application', 'parent', 'attachedtemplate',
  'normaltemplate', 'templates', 'listtemplategalleries', 'bibliography', 'buildingblockentries', 'hyperlinks',
  'followhyperlink', 'doclick', 'insertfile', 'addpicture', 'addoleobject', 'addolecontrol', 'linkformat', 'mailmerge',
  'sendmail', 'sendfax', 'save', 'saveas', 'saveas2', 'savecopyas', 'exportasfixedformat', 'printout',
  'printpreview', 'close', 'reload', 'compare', 'run', 'vbproject', 'filesystem', 'env', 'shell']);
/** ``WdFieldType`` of the fields above, and of a hyperlink, which is kept out of reach too. */
const FETCHING_FIELD_TYPES = new Set([36, 45, 46, 55, 56, 67, 68, 88]);
const FETCHING_FIELD_CODE = /^\s*(?:INCLUDE|INCLUDETEXT|INCLUDEPICTURE|LINK|DDE|DDEAUTO|IMPORT|HYPERLINK)\b/i;

/**
 * The runner's screen for WPS Writer: another file merged in, and fields and inserted markup that fetch from outside
 * the document.
 */
export function screenWpsWriterWrite({ path, name, values }: HostWrite): string | undefined {
  // A table's cells merge with a cell; a document merges with a file, named by its path.
  if (/^merge$/i.test(name) && typeof values[0] === 'string') {
    return 'Code here works on this document only; it cannot read another file into it.';
  }
  const field = /^add$/i.test(name) && /^fields$/i.test(path.at(-1) ?? '');
  if ((field && FETCHING_FIELD_TYPES.has(Number(values[1])))
    || ((field || path.some(step => /^(?:fields|code)$/i.test(step)))
      && values.some(value => typeof value === 'string' && FETCHING_FIELD_CODE.test(value)))) {
    return 'The field would make WPS fetch content from another file, program or address.';
  }
  if (/^insertxml$/i.test(name) && values.some(value => typeof value === 'string' && FETCHING_MARKUP.test(value))) {
    return 'The inserted content would make WPS fetch from another file, program or address.';
  }
  // Updating runs every field in reach, including ones already in the document that fetch from outside it.
  if (/^update(?:source)?$/i.test(name) && path.some(step => /^fields?$/i.test(step))) {
    return 'Fields cannot be updated here: one may fetch content from another file, program or address.';
  }
  return undefined;
}
