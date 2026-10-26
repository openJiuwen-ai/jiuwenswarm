import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import ts from 'typescript';

const root = new URL('..', import.meta.url);
const sourceUrl = new URL('src/features/a2ui/actionDefaults.ts', root);
const helperUrl = new URL('src/features/a2ui/formDefaults.ts', root);
const tempDir = await mkdtemp(join(tmpdir(), 'a2ui-action-defaults-'));

async function transpileTsModule(url, outputFileName) {
  const source = await readFile(url, 'utf8');
  const transpiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2020,
      target: ts.ScriptTarget.ES2020,
      importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
    },
  });
  const outputPath = join(tempDir, outputFileName);
  const outputText = outputFileName === 'actionDefaults.mjs'
    ? transpiled.outputText.replaceAll("from './formDefaults'", "from './formDefaults.mjs'")
    : transpiled.outputText;
  await writeFile(outputPath, outputText, 'utf8');
  return outputPath;
}

async function importTsModule(url) {
  await transpileTsModule(helperUrl, 'formDefaults.mjs');
  const outputPath = await transpileTsModule(url, 'actionDefaults.mjs');
  return import(`file://${outputPath.replace(/\\/g, '/')}`);
}

const {
  clearA2UIActionDefaults,
  enrichA2UIClientEventWithDefaults,
  recordA2UIActionDefaults,
} = await importTsModule(sourceUrl);
const helperOutputPath = await transpileTsModule(helperUrl, 'formDefaults.mjs');
const {
  isMultiSelectChoice,
  isSingleSelectChoice,
  nextChoiceSelections,
  selectionCollectionValues,
} = await import(`file://${helperOutputPath.replace(/\\/g, '/')}?choice-helpers`);

assert.equal(
  isMultiSelectChoice({ variant: 'chips', maxAllowedSelections: 1 }),
  true,
  'chips controls should retain their multi-choice visual renderer',
);
assert.equal(
  isSingleSelectChoice({ variant: 'chips', maxAllowedSelections: 1 }),
  true,
  'chips appearance must not override maxAllowedSelections=1 semantics',
);
assert.equal(
  isSingleSelectChoice({ variant: 'checkbox', maxAllowedSelections: 1 }),
  true,
  'checkbox appearance must not override maxAllowedSelections=1 semantics',
);
assert.deepEqual(
  nextChoiceSelections(['very_spicy'], 'mild', true, 1),
  ['mild'],
  'single-select chips should replace the previous selection',
);
assert.deepEqual(
  nextChoiceSelections(['very_spicy'], 'mild', true, 2),
  ['very_spicy', 'mild'],
  'multi-select chips should add a second selection within the limit',
);
assert.deepEqual(
  nextChoiceSelections(['very_spicy', 'mild'], 'no_spicy', true, 2),
  ['very_spicy', 'mild'],
  'multi-select chips should keep their previous value when the limit is exceeded',
);
assert.deepEqual(
  nextChoiceSelections(['very_spicy', 'mild'], 'mild', false),
  ['very_spicy'],
  'multi-select chips should remove an unchecked selection',
);
assert.deepEqual(
  selectionCollectionValues(new Map()),
  [],
  'an empty A2UI valueMap must not consume a hidden selection slot',
);
assert.deepEqual(
  selectionCollectionValues(new Map([
    ['0', 'chinese'],
    ['1', 'japanese'],
  ])),
  ['chinese', 'japanese'],
  'A2UI valueMap selections should be read from their values',
);
const fiveCuisineSelections = ['chinese', 'japanese', 'western', 'mexican']
  .reduce(
    (selected, cuisine) => nextChoiceSelections(selected, cuisine, true, 5),
    selectionCollectionValues(new Map()),
  );
assert.deepEqual(
  nextChoiceSelections(fiveCuisineSelections, 'italian', true, 5),
  ['chinese', 'japanese', 'western', 'mexican', 'italian'],
  'an empty valueMap should allow all five visible selections',
);
assert.deepEqual(
  nextChoiceSelections(
    ['chinese', 'japanese', 'western', 'mexican', 'italian'],
    'korean',
    true,
    5,
  ),
  ['chinese', 'japanese', 'western', 'mexican', 'italian'],
  'the sixth visible selection should still be rejected',
);

function selectionMessages(defaultValue = 'chinese') {
  return [
    { beginRendering: { surfaceId: 'surface-1', root: 'root' } },
    {
      surfaceUpdate: {
        surfaceId: 'surface-1',
        components: [
          {
            id: 'food-choice',
            component: {
              MultipleChoice: {
                selections: { path: '/food/type' },
                options: [
                  { label: { literalString: '中餐' }, value: defaultValue },
                  { label: { literalString: '西餐' }, value: 'western' },
                ],
              },
            },
          },
          {
            id: 'submit',
            component: {
              Button: {
                child: 'submit-label',
                action: {
                  name: 'submit_form',
                  context: [
                    { key: 'foodType', value: { path: '/food/type' } },
                  ],
                },
              },
            },
          },
        ],
      },
    },
  ];
}

clearA2UIActionDefaults();
recordA2UIActionDefaults(selectionMessages());

assert.deepEqual(
  enrichA2UIClientEventWithDefaults({
    userAction: {
      name: 'submit_form',
      sourceComponentId: 'submit',
      surfaceId: 'surface-1',
      timestamp: '2026-05-30T00:00:00.000Z',
      context: { foodType: null },
    },
  }).userAction.context,
  { foodType: ['chinese'] },
  'unmodified visible default should be sent instead of null',
);

assert.deepEqual(
  enrichA2UIClientEventWithDefaults({
    userAction: {
      name: 'submit_form',
      sourceComponentId: 'submit',
      surfaceId: 'surface-1',
      timestamp: '2026-05-30T00:00:00.000Z',
      context: { foodType: [] },
    },
  }).userAction.context,
  { foodType: ['chinese'] },
  'same-option selection that leaves an empty array should use visible default',
);

assert.deepEqual(
  enrichA2UIClientEventWithDefaults({
    userAction: {
      name: 'submit_form',
      sourceComponentId: 'submit',
      surfaceId: 'surface-1',
      timestamp: '2026-05-30T00:00:00.000Z',
      context: { foodType: {} },
    },
  }).userAction.context,
  { foodType: ['chinese'] },
  'choice context represented as an empty object should use visible default',
);

assert.deepEqual(
  enrichA2UIClientEventWithDefaults({
    userAction: {
      name: 'submit_form',
      sourceComponentId: 'submit',
      surfaceId: 'surface-1',
      timestamp: '2026-05-30T00:00:00.000Z',
      context: { foodType: 'western' },
    },
  }).userAction.context,
  { foodType: 'western' },
  'non-empty user selection should not be overwritten',
);

clearA2UIActionDefaults();
recordA2UIActionDefaults([
  {
    surfaceUpdate: {
      surfaceId: 'surface-1',
      components: [
        {
          id: 'priority-choice',
          component: {
            SingleChoice: {
              selections: { path: '/priority' },
              options: [
                { label: { literalString: 'Low' }, value: 'low' },
                { label: { literalString: 'High' }, value: 'high' },
              ],
            },
          },
        },
        {
          id: 'submit',
          component: {
            Button: {
              child: 'submit-label',
              action: {
                name: 'submit_form',
                context: [{ key: 'priority', value: { path: '/priority' } }],
              },
            },
          },
        },
      ],
    },
  },
]);

assert.deepEqual(
  enrichA2UIClientEventWithDefaults({
    userAction: {
      name: 'submit_form',
      sourceComponentId: 'submit',
      surfaceId: 'surface-1',
      timestamp: '2026-05-30T00:00:00.000Z',
      context: { priority: {} },
    },
  }).userAction.context,
  { priority: ['low'] },
  'any choice-like component with options and selections.path should provide a visible default',
);

clearA2UIActionDefaults();
recordA2UIActionDefaults([
  {
    surfaceUpdate: {
      surfaceId: 'surface-1',
      components: [
        {
          id: 'rating-choice',
          component: {
            SingleChoice: {
              selections: { path: '/rating' },
              options: [
                { label: { literalString: 'One' }, value: 1 },
                { label: { literalString: 'Two' }, value: 2 },
              ],
            },
          },
        },
        {
          id: 'submit',
          component: {
            Button: {
              child: 'submit-label',
              action: {
                name: 'submit_form',
                context: [{ key: 'rating', value: { path: '/rating' } }],
              },
            },
          },
        },
      ],
    },
  },
]);

assert.deepEqual(
  enrichA2UIClientEventWithDefaults({
    userAction: {
      name: 'submit_form',
      sourceComponentId: 'submit',
      surfaceId: 'surface-1',
      timestamp: '2026-05-30T00:00:00.000Z',
      context: { rating: {} },
    },
  }).userAction.context,
  { rating: [1] },
  'choice defaults should preserve non-string option values',
);

clearA2UIActionDefaults();
recordA2UIActionDefaults([
  {
    surfaceUpdate: {
      surfaceId: 'surface-1',
      components: [
        {
          id: 'food-choice',
          component: {
            MultipleChoice: {
              selections: { path: '/food/type', literalArray: ['fallback'] },
              options: [],
            },
          },
        },
        {
          id: 'submit',
          component: {
            Button: {
              child: 'submit-label',
              action: {
                name: 'submit_form',
                context: [{ key: 'foodType', value: { path: '/food/type' } }],
              },
            },
          },
        },
      ],
    },
  },
]);

assert.deepEqual(
  enrichA2UIClientEventWithDefaults({
    userAction: {
      name: 'submit_form',
      sourceComponentId: 'submit',
      surfaceId: 'surface-1',
      timestamp: '2026-05-30T00:00:00.000Z',
      context: {},
    },
  }).userAction.context,
  { foodType: ['fallback'] },
  'literalArray should be used when no option is visible',
);

clearA2UIActionDefaults();
recordA2UIActionDefaults([
  {
    surfaceUpdate: {
      surfaceId: 'surface-1',
      components: [
        {
          id: 'food-choice',
          component: {
            MultipleChoice: {
              selections: { path: '/food/type' },
              options: [
                { label: { literalString: 'Chinese' }, value: 'chinese' },
                { label: { literalString: 'Western' }, value: 'western' },
              ],
            },
          },
        },
      ],
    },
  },
]);
recordA2UIActionDefaults([
  {
    surfaceUpdate: {
      surfaceId: 'surface-1',
      components: [
        {
          id: 'submit',
          component: {
            Button: {
              child: 'submit-label',
              action: {
                name: 'submit_form',
                context: [{ key: 'foodType', value: { path: '/food/type' } }],
              },
            },
          },
        },
      ],
    },
  },
]);

assert.deepEqual(
  enrichA2UIClientEventWithDefaults({
    userAction: {
      name: 'submit_form',
      sourceComponentId: 'submit',
      surfaceId: 'surface-1',
      timestamp: '2026-05-30T00:00:00.000Z',
      context: { foodType: null },
    },
  }).userAction.context,
  { foodType: ['chinese'] },
  'defaults recorded in an earlier surface update should be available to later buttons',
);

clearA2UIActionDefaults();
recordA2UIActionDefaults([
  {
    surfaceUpdate: {
      surfaceId: 'surface-1',
      components: [
        {
          id: 'multi-choice',
          component: {
            MultipleChoice: {
              variant: 'checkbox',
              selections: { path: '/foods' },
              options: [
                { label: { literalString: 'Chinese' }, value: 'chinese' },
                { label: { literalString: 'Western' }, value: 'western' },
              ],
            },
          },
        },
        {
          id: 'submit',
          component: {
            Button: {
              child: 'submit-label',
              action: {
                name: 'submit_form',
                context: [{ key: 'foods', value: { path: '/foods' } }],
              },
            },
          },
        },
      ],
    },
  },
]);

assert.deepEqual(
  enrichA2UIClientEventWithDefaults({
    userAction: {
      name: 'submit_form',
      sourceComponentId: 'submit',
      surfaceId: 'surface-1',
      timestamp: '2026-05-30T00:00:00.000Z',
      context: { foods: null },
    },
  }).userAction.context,
  { foods: null },
  'checkbox-style MultipleChoice should not submit the first option as an invisible default',
);

clearA2UIActionDefaults();
recordA2UIActionDefaults([
  {
    surfaceUpdate: {
      surfaceId: 'surface-1',
      components: [
        {
          id: 'form-field',
          component: {
            TextField: {
              text: { path: '/name', literalString: 'Alice' },
            },
          },
        },
        {
          id: 'slider-field',
          component: {
            Slider: {
              value: { path: '/age', literalNumber: 30 },
            },
          },
        },
        {
          id: 'checkbox-field',
          component: {
            CheckBox: {
              value: { path: '/accepted', literalBoolean: true },
            },
          },
        },
        {
          id: 'date-field',
          component: {
            DateTimeInput: {
              value: { path: '/date', literalString: '2026-06-14' },
            },
          },
        },
        {
          id: 'submit',
          component: {
            Button: {
              child: 'submit-label',
              action: {
                name: 'submit_form',
                context: [
                  { key: 'name', value: { path: '/name' } },
                  { key: 'age', value: { path: '/age' } },
                  { key: 'accepted', value: { path: '/accepted' } },
                  { key: 'date', value: { path: '/date' } },
                ],
              },
            },
          },
        },
      ],
    },
  },
]);

assert.deepEqual(
  enrichA2UIClientEventWithDefaults({
    userAction: {
      name: 'submit_form',
      sourceComponentId: 'submit',
      surfaceId: 'surface-1',
      timestamp: '2026-05-30T00:00:00.000Z',
      context: { name: null, age: null, accepted: null, date: null },
    },
  }).userAction.context,
  { name: 'Alice', age: 30, accepted: true, date: '2026-06-14' },
  'literal defaults for TextField, Slider, CheckBox, and DateTimeInput should be submitted',
);

await rm(tempDir, { recursive: true, force: true });
