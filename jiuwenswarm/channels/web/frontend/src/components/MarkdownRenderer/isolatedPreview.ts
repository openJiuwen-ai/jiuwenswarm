export const UNTRUSTED_STATIC_PREVIEW_SANDBOX = 'allow-same-origin';

export const UNTRUSTED_STATIC_PREVIEW_CSP = [
  "default-src 'none'",
  'img-src data: blob:',
  "style-src 'unsafe-inline'",
  'font-src data:',
  "form-action 'none'",
  "base-uri 'none'",
].join('; ');

interface StaticPreviewDocumentOptions {
  styles: string;
}

export function createStaticPreviewDocument({ styles }: StaticPreviewDocumentOptions): string {
  return `<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="${UNTRUSTED_STATIC_PREVIEW_CSP}">
    <style>${styles}</style>
  </head>
  <body></body>
</html>`;
}
