/** Text from the document as a state block quotes it. */

/**
 * ``text`` as a JSON string, cut to ``max`` characters with ``…``. A ``<`` is written ``<``, which reads the
 * same as JSON, so quoted text cannot close or open a block such as ``</untrusted_content>``.
 */
export function quoted(text: string, max: number): string {
  const chars = [...text];
  return JSON.stringify(chars.length > max ? `${chars.slice(0, max).join('')}…` : text).replace(/</g, '\\u003c');
}
