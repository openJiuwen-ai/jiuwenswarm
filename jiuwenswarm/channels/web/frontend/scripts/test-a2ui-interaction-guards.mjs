import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import ts from 'typescript';

const root = new URL('..', import.meta.url);
const tempDir = await mkdtemp(join(tmpdir(), 'a2ui-interaction-guards-'));

const moduleMap = new Map([
  ['src/features/a2ui/a2uiContent.ts', 'a2uiContent.mjs'],
  ['src/features/a2ui/actionBridge.ts', 'actionBridge.mjs'],
  ['src/features/a2ui/actionDefaults.ts', 'actionDefaults.mjs'],
  ['src/features/a2ui/featureConfig.ts', 'featureConfig.mjs'],
  ['src/features/a2ui/formDefaults.ts', 'formDefaults.mjs'],
]);

function rewriteLocalImports(source) {
  return source
    .replaceAll("from './a2uiContent'", "from './a2uiContent.mjs'")
    .replaceAll("from './actionDefaults'", "from './actionDefaults.mjs'")
    .replaceAll("from './featureConfig'", "from './featureConfig.mjs'")
    .replaceAll("from './formDefaults'", "from './formDefaults.mjs'");
}

async function transpileTsModule(sourcePath, outputFileName) {
  const sourceUrl = new URL(sourcePath, root);
  const source = await readFile(sourceUrl, 'utf8');
  const transpiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2020,
      target: ts.ScriptTarget.ES2020,
      importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
    },
  });
  const outputPath = join(tempDir, outputFileName);
  await writeFile(outputPath, rewriteLocalImports(transpiled.outputText), 'utf8');
  return outputPath;
}

async function importTsModule(sourcePath) {
  for (const [path, output] of moduleMap.entries()) {
    await transpileTsModule(path, output);
  }
  const outputPath = join(tempDir, moduleMap.get(sourcePath));
  return import(`file://${outputPath.replace(/\\/g, '/')}`);
}

try {
  const { namespaceA2UIMessages } = await importTsModule('src/features/a2ui/a2uiContent.ts');
  const plainNamespaced = namespaceA2UIMessages(
    [{ beginRendering: { surfaceId: 'surface-1', root: 'root' } }],
    'msg_new',
  );
  assert.equal(
    plainNamespaced[0].beginRendering.surfaceId,
    'msg_new:surface-1',
    'plain surface ids should be scoped to the current message namespace',
  );

  const alreadyNamespaced = namespaceA2UIMessages(
    [{ surfaceUpdate: { surfaceId: 'msg_old:surface-1', components: [] } }],
    'msg_new',
  );
  assert.equal(
    alreadyNamespaced[0].surfaceUpdate.surfaceId,
    'msg_old:surface-1',
    'surface ids that already carry a message namespace must not be scoped twice',
  );

  const {
    dispatchA2UIAction,
    setA2UIActionHandler,
  } = await importTsModule('src/features/a2ui/actionBridge.ts');

  let finishFirst;
  const firstCompletion = new Promise((resolve) => {
    finishFirst = resolve;
  });
  const handled = [];
  const cleanup = setA2UIActionHandler(async (message) => {
    handled.push(message.userAction?.sourceComponentId);
    if (handled.length === 1) {
      await firstCompletion;
    }
  });

  const baseAction = {
    userAction: {
      name: 'showHello',
      surfaceId: 'msg_old:surface-1',
      sourceComponentId: 'helloButton',
      context: {},
      timestamp: '2026-06-22T00:00:00.000Z',
    },
  };

  const firstDispatch = dispatchA2UIAction(baseAction);
  await dispatchA2UIAction(baseAction);
  assert.deepEqual(
    handled,
    ['helloButton'],
    'duplicate clicks for the same in-flight A2UI action should be ignored',
  );

  await dispatchA2UIAction({
    userAction: {
      ...baseAction.userAction,
      sourceComponentId: 'otherButton',
    },
  });
  assert.deepEqual(
    handled,
    ['helloButton', 'otherButton'],
    'different A2UI action keys should still be allowed while one key is in flight',
  );

  finishFirst();
  await firstDispatch;
  await dispatchA2UIAction(baseAction);
  assert.deepEqual(
    handled,
    ['helloButton', 'otherButton', 'helloButton'],
    'the same A2UI action key should be allowed again after its request settles',
  );

  cleanup();

  const {
    resolveA2UITextValue,
  } = await importTsModule('src/features/a2ui/formDefaults.ts');

  assert.equal(
    resolveA2UITextValue({ literalString: 'helloworld' }, () => null),
    'helloworld',
    'literal Text values should still render normally',
  );
  assert.equal(
    resolveA2UITextValue({ path: '/message' }, () => new Map([['text', 'helloworld']])),
    'helloworld',
    'Text paths that resolve to a single scalar inside a Map should unwrap that scalar',
  );
  assert.equal(
    resolveA2UITextValue({ path: '/message' }, () => new Map([['title', 'A'], ['body', 'B']])),
    null,
    'Text paths that resolve to an object-like Map must not render as [object Map]',
  );
} finally {
  await rm(tempDir, { recursive: true, force: true });
}
