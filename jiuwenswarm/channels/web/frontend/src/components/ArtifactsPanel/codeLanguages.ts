import type { Extension } from '@codemirror/state';
import { StreamLanguage } from '@codemirror/language';
import { fileExtension, isCodeLanguageExtension, type CodeLanguageExtension } from './codeLanguageExtensions';

type LanguageLoader = () => Promise<Extension>;

const javascript = async (typescript = false, jsx = false): Promise<Extension> => (await import('@codemirror/lang-javascript')).javascript({ typescript, jsx });

const cpp = async (): Promise<Extension> => (await import('@codemirror/lang-cpp')).cpp();

const LANGUAGE_LOADERS = {
  bash: async () => StreamLanguage.define((await import('@codemirror/legacy-modes/mode/shell')).shell),
  c: cpp,
  cc: cpp,
  cjs: () => javascript(),
  cpp,
  cs: async () => StreamLanguage.define((await import('@codemirror/legacy-modes/mode/clike')).csharp),
  css: async () => (await import('@codemirror/lang-css')).css(),
  go: async () => (await import('@codemirror/lang-go')).go(),
  h: cpp,
  hpp: cpp,
  java: async () => (await import('@codemirror/lang-java')).java(),
  js: () => javascript(),
  jsx: () => javascript(false, true),
  kt: async () => StreamLanguage.define((await import('@codemirror/legacy-modes/mode/clike')).kotlin),
  kts: async () => StreamLanguage.define((await import('@codemirror/legacy-modes/mode/clike')).kotlin),
  lua: async () => StreamLanguage.define((await import('@codemirror/legacy-modes/mode/lua')).lua),
  mjs: () => javascript(),
  php: async () => (await import('@codemirror/lang-php')).php(),
  pl: async () => StreamLanguage.define((await import('@codemirror/legacy-modes/mode/perl')).perl),
  py: async () => (await import('@codemirror/lang-python')).python(),
  rb: async () => StreamLanguage.define((await import('@codemirror/legacy-modes/mode/ruby')).ruby),
  rs: async () => (await import('@codemirror/lang-rust')).rust(),
  sh: async () => StreamLanguage.define((await import('@codemirror/legacy-modes/mode/shell')).shell),
  sql: async () => (await import('@codemirror/lang-sql')).sql(),
  swift: async () => StreamLanguage.define((await import('@codemirror/legacy-modes/mode/swift')).swift),
  toml: async () => StreamLanguage.define((await import('@codemirror/legacy-modes/mode/toml')).toml),
  ts: () => javascript(true),
  tsx: () => javascript(true, true),
  vue: async () => (await import('@codemirror/lang-vue')).vue(),
  xml: async () => (await import('@codemirror/lang-xml')).xml(),
  yaml: async () => (await import('@codemirror/lang-yaml')).yaml(),
  yml: async () => (await import('@codemirror/lang-yaml')).yaml(),
} satisfies Record<CodeLanguageExtension, LanguageLoader>;

export async function loadCodeLanguage(name: string, mimeType?: string): Promise<Extension> {
  const extension = fileExtension(name);
  if (isCodeLanguageExtension(extension)) return LANGUAGE_LOADERS[extension]();

  const mime = (mimeType ?? '').toLowerCase();
  if (mime === 'application/typescript') return LANGUAGE_LOADERS.ts();
  if (mime === 'application/javascript' || mime === 'text/javascript') return LANGUAGE_LOADERS.js();
  throw new Error(`unsupported_code_language:${extension || mime || 'unknown'}`);
}
