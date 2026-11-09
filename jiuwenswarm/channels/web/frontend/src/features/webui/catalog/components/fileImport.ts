import { array, bool, component, object, text } from './shared.ts';
import { hasNonEmptyLocalizedText } from './localizedText.ts';

/** One `accept` entry such as `.zip`; the Gateway FILE_SUFFIX applies the same pattern. */
const FILE_SUFFIX = /^\.[A-Za-z0-9][A-Za-z0-9._-]{0,15}$/;
export const UPLOAD_ID = /^[0-9a-f]{32}$/;

export const fileImportRule = component('FileImport', {
  label: { kind: 'union', choices: [{ kind: 'string', maxBytes: 200 },
    object({ zh: { kind: 'string', maxBytes: 200 }, en: { kind: 'string', maxBytes: 200 } }, ['zh', 'en'])] }, accept: array(text),
  multiple: bool, directory: bool, disabled: bool,
}, {
  required: ['label'],
  events: { 'ui.change': object({ upload_id: text }, ['upload_id']) },
  validate(props) {
    if (!hasNonEmptyLocalizedText(props.label)) return 'FileImport label is required';
    const accept = props.accept as string[] | undefined;
    if (accept !== undefined && (accept.length < 1 || accept.length > 16 || !accept.every((item) => FILE_SUFFIX.test(item))
      || new Set(accept.map((item) => item.toLowerCase())).size !== accept.length)) return 'invalid FileImport accept';
    if (props.directory === true && (accept !== undefined || props.multiple !== undefined)) {
      return 'directory import does not take accept or multiple';
    }
    return null;
  },
  validateEvent(_event, data) {
    return typeof data.upload_id === 'string' && UPLOAD_ID.test(data.upload_id) ? null : 'invalid upload_id';
  },
});
