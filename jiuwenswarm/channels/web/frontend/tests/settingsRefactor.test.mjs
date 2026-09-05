import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import ts from 'typescript';

import {
  buildConfigSavePayload,
  buildModelValidationPayload,
  buildModelsSavePayload,
  normalizeSettingsConfigUpdates,
  normalizePermissionLevel,
  SETTINGS_CONFIG_FIELDS,
} from '../node_modules/.cache/settings-refactor/services/settingsContract.js';
import { SettingsSaveQueue } from '../node_modules/.cache/settings-refactor/services/SettingsSaveQueue.js';
import { createSettingsRequestRouter } from '../node_modules/.cache/settings-refactor/services/createSettingsRequestRouter.js';
import {
  createSettingsPageDefinition,
  validateSettingsI18n,
} from '../node_modules/.cache/settings-refactor/registry/createSettingsPageDefinition.js';
import {
  buildSettingsPageDefinition,
  restrictSettingsAccess,
} from '../node_modules/.cache/settings-refactor/registry/buildSettingsPageDefinition.js';
import { openSourceSettingsAccessPolicy } from '../node_modules/.cache/settings-refactor/registry/accessPolicy.js';
import {
  isMediaCapabilityConfigured,
  mediaCapabilityModalities,
  mediaCapabilityConfigFields,
  mediaCapabilityEnabledField,
  mediaCapabilityPersistenceFields,
  wasConfigAppliedWithoutRestart,
} from '../node_modules/.cache/settings-refactor/modules/agent/mediaCapabilities.js';
import {
  buildMediaModelConfigUpdates,
  createMediaModelDraft,
} from '../node_modules/.cache/settings-refactor/modules/agent/mediaModelConfig.js';

const root = new URL('../', import.meta.url);
const source = (path) => readFileSync(new URL(path, root), 'utf8');

function sourceFilesUnder(path) {
  const walk = (directory) =>
    readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
      const target = new URL(entry.name, directory);
      if (entry.isDirectory()) return walk(new URL(`${entry.name}/`, directory));
      return /\.(?:css|html|ts|tsx)$/.test(entry.name) ? [target] : [];
    });
  return walk(new URL(`${path.replace(/\/?$/, '/')}`, root));
}
const zh = JSON.parse(source('src/i18n/locales/zh.json'));
const en = JSON.parse(source('src/i18n/locales/en.json'));

function leafKeys(value, prefix = '') {
  return Object.entries(value).flatMap(([key, child]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return child && typeof child === 'object' && !Array.isArray(child) ? leafKeys(child, path) : [path];
  });
}

function parseTsx(path) {
  return ts.createSourceFile(path, source(path), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
}

function unwrapExpression(expression) {
  let current = expression;
  while (
    current &&
    (ts.isAsExpression(current) || ts.isSatisfiesExpression(current) || ts.isParenthesizedExpression(current))
  ) {
    current = current.expression;
  }
  return current;
}

function findVariableArrayStrings(file, variableName) {
  let values;
  const visit = (node) => {
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.name.text === variableName &&
      node.initializer &&
      ts.isArrayLiteralExpression(unwrapExpression(node.initializer))
    ) {
      values = unwrapExpression(node.initializer).elements.map((element) => {
        assert.equal(ts.isStringLiteral(element), true, `${variableName} must contain only string literals`);
        return element.text;
      });
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  assert.ok(values, `${variableName} must exist`);
  return values;
}

function findVariableArrayObjectStringProperty(file, variableName, propertyName) {
  let values;
  const visit = (node) => {
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.name.text === variableName &&
      node.initializer &&
      ts.isArrayLiteralExpression(unwrapExpression(node.initializer))
    ) {
      values = unwrapExpression(node.initializer).elements.map((element) => {
        assert.equal(ts.isObjectLiteralExpression(element), true, `${variableName} entries must be objects`);
        const property = element.properties.find(
          (candidate) => ts.isPropertyAssignment(candidate) && candidate.name.getText() === propertyName,
        );
        assert.ok(property && ts.isPropertyAssignment(property), `${variableName}.${propertyName} must exist`);
        assert.equal(
          ts.isStringLiteral(property.initializer),
          true,
          `${variableName}.${propertyName} must be a string`,
        );
        return property.initializer.text;
      });
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  assert.ok(values, `${variableName} must exist`);
  return values;
}

function findReturnedObjectKeys(file, functionName) {
  let keys;
  const visit = (node) => {
    if (ts.isFunctionDeclaration(node) && node.name?.text === functionName && node.body) {
      const findReturn = (child) => {
        if (ts.isReturnStatement(child) && child.expression && ts.isObjectLiteralExpression(child.expression)) {
          keys = child.expression.properties.map((property) => {
            assert.equal(
              ts.isPropertyAssignment(property) || ts.isShorthandPropertyAssignment(property),
              true,
              `${functionName} must return explicit object properties`,
            );
            assert.equal(ts.isIdentifier(property.name), true, `${functionName} keys must be identifiers`);
            return property.name.text;
          });
          return;
        }
        ts.forEachChild(child, findReturn);
      };
      findReturn(node.body);
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  assert.ok(keys, `${functionName} must return an object literal`);
  return keys;
}

function findRequestMethods(file) {
  const methods = new Set();
  const visit = (node) => {
    if (
      ts.isCallExpression(node) &&
      ts.isIdentifier(node.expression) &&
      ['request', 'webRequest'].includes(node.expression.text) &&
      node.arguments[0] &&
      ts.isStringLiteral(node.arguments[0])
    ) {
      methods.add(node.arguments[0].text);
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return methods;
}

function findJsxStringAttributeValues(file, elementName, attributeName) {
  const values = [];
  const visit = (node) => {
    if (ts.isJsxSelfClosingElement(node) && node.tagName.getText() === elementName) {
      const attribute = node.attributes.properties.find(
        (candidate) => ts.isJsxAttribute(candidate) && candidate.name.text === attributeName,
      );
      assert.ok(attribute && ts.isJsxAttribute(attribute), `${elementName}.${attributeName} must exist`);
      assert.equal(
        attribute.initializer && ts.isStringLiteral(attribute.initializer),
        true,
        `${elementName}.${attributeName} must be a string literal`,
      );
      values.push(attribute.initializer.text);
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return values;
}

function findSettingDefinitions(file) {
  const definitions = [];
  const visit = (node) => {
    if (ts.isObjectLiteralExpression(node)) {
      const properties = new Map(
        node.properties
          .filter((property) => ts.isPropertyAssignment(property))
          .map((property) => [property.name.getText().replaceAll(/["']/g, ''), property.initializer]),
      );
      const component = properties.get('component');
      const key = properties.get('key');
      if (
        component &&
        ts.isStringLiteral(component) &&
        ['switch', 'select', 'input'].includes(component.text) &&
        key &&
        ts.isStringLiteral(key)
      ) {
        definitions.push({ component: component.text, key: key.text });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return definitions;
}

function findSettingDefinitionKeys(file) {
  return findSettingDefinitions(file).map(({ key }) => key);
}

function findLiteralTranslationKeys(file) {
  const keys = new Set();
  const visit = (node) => {
    if (
      ts.isCallExpression(node) &&
      ts.isIdentifier(node.expression) &&
      node.expression.text === 't' &&
      node.arguments[0] &&
      ts.isStringLiteral(node.arguments[0])
    ) {
      keys.add(node.arguments[0].text);
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return [...keys];
}

function translationAt(locale, key) {
  return key.split('.').reduce((value, part) => value?.[part], locale);
}

test('registry definition preserves the fixed six-module order and fails invalid registrations', () => {
  const definition = source('src/features/settings/registry/openSourceDefinition.ts');
  assert.match(
    definition,
    /generalModule,\s*modelsModule,\s*agentModule,\s*browserModule,\s*channelsModule,\s*experimentalModule/,
  );
  assert.doesNotMatch(definition, /securityModule/);
  for (const removedPath of [
    'src/features/settings/modules/security/SecuritySettings.tsx',
    'src/features/settings/modules/security/definition.ts',
    'src/features/settings/modules/security/index.ts',
    'src/assets/settings/navigation/security.svg',
    'src/features/settings/modules/memory/MemorySettings.tsx',
    'src/features/settings/modules/memory/definition.ts',
    'src/features/settings/modules/memory/index.ts',
    'src/assets/settings/navigation/memory.svg',
  ]) {
    assert.equal(existsSync(new URL(removedPath, root)), false, `${removedPath} must not exist`);
  }
  assert.throws(
    () =>
      createSettingsPageDefinition({
        id: 'test',
        compositionMode: 'base',
        accessPolicy: { evaluate: () => ({ level: 'editable' }) },
        modules: [],
      }),
    /at least one module/,
  );
  const item = () => null;
  assert.throws(
    () =>
      createSettingsPageDefinition({
        id: 'test',
        compositionMode: 'base',
        accessPolicy: { evaluate: () => ({ level: 'editable' }) },
        modules: [
          { id: 'a', titleKey: 'a', icon: item, sections: [] },
          { id: 'a', titleKey: 'b', icon: item, sections: [] },
        ],
      }),
    /Duplicate module id/,
  );
  assert.throws(
    () =>
      createSettingsPageDefinition({
        id: 'test',
        compositionMode: 'base',
        accessPolicy: { evaluate: () => ({ level: 'editable' }) },
        modules: [
          {
            id: 'a',
            titleKey: 'a',
            icon: item,
            sections: [{ id: 's', items: [{ id: 'item', component: 'custom', render: null }] }],
          },
        ],
      }),
    /missing a custom component/,
  );
  const localizedDefinition = createSettingsPageDefinition({
    id: 'localized',
    compositionMode: 'base',
    accessPolicy: { evaluate: () => ({ level: 'editable' }) },
    modules: [
      {
        id: 'a',
        titleKey: 'settings.a',
        descriptionKey: 'settings.aDescription',
        icon: item,
        sections: [
          {
            id: 's',
            titleKey: 'settings.section',
            descriptionKey: 'settings.sectionDescription',
            items: [{ id: 'item', component: 'custom', render: item }],
          },
        ],
      },
    ],
  });
  assert.throws(
    () => validateSettingsI18n(localizedDefinition, (key) => key === 'settings.a'),
    /Missing settings i18n key: settings\.aDescription/,
  );
  assert.throws(
    () =>
      validateSettingsI18n(localizedDefinition, (key) =>
        ['settings.a', 'settings.aDescription', 'settings.section'].includes(key),
      ),
    /Missing settings i18n key: settings\.sectionDescription/,
  );
});

test('current Settings titles omit descriptions while the shared API retains optional support', () => {
  const descriptionKeys = [
    'settingsPanel.moduleDescriptions',
    'settingsPanel.models.freeModelsDescription',
    'settingsPanel.models.primaryModelsDescription',
    'settingsPanel.agent.skillsDescription',
    'settingsPanel.agent.webSearchDescription',
    'settingsPanel.agent.mediaToolsDescription',
    'settingsPanel.agent.teamDescription',
    'settingsPanel.experimental.externalCliAgentsDescription',
    'settingsPanel.experimental.a2uiDescription',
    'settingsPanel.experimental.proactiveDescription',
  ];
  for (const locale of [zh, en]) {
    for (const key of descriptionKeys) assert.equal(translationAt(locale, key), undefined, `${key} must be absent`);
  }

  const registrySources = [
    'src/features/settings/modules/general/definition.ts',
    'src/features/settings/modules/models/definition.ts',
    'src/features/settings/modules/agent/definition.ts',
    'src/features/settings/modules/browser/definition.ts',
    'src/features/settings/modules/experimental/definition.ts',
  ];
  for (const path of registrySources)
    assert.doesNotMatch(source(path), /\bdescriptionKey\b/, `${path} must not expose descriptions`);
  const registryTypes = source('src/features/settings/registry/types.ts');
  const pageLayout = source('src/features/settings/SettingsPageLayout.tsx');
  const section = source('src/features/settings/components/SettingsSection.tsx');
  assert.match(registryTypes, /interface SettingsSectionDefinition[\s\S]*descriptionKey\?: I18nKey/);
  assert.match(registryTypes, /interface SettingsModuleDefinition[\s\S]*descriptionKey\?: I18nKey/);
  assert.match(pageLayout, /active\.module\.descriptionKey/);
  assert.match(pageLayout, /section\.descriptionKey/);
  assert.match(section, /description\?: ReactNode/);
  assert.match(section, /description \? <p>\{description\}<\/p>/);
});

test('simple Settings definitions reject unknown sources and derive required i18n keys', () => {
  const Icon = () => null;
  const create = (module) =>
    createSettingsPageDefinition({
      id: 'simple-settings',
      compositionMode: 'base',
      accessPolicy: { evaluate: () => ({ level: 'editable' }) },
      modules: [module],
    });
  assert.throws(
    () =>
      create({
        id: 'agent',
        titleKey: 'settings.agent',
        icon: Icon,
        sections: [{ id: 'agent', items: [{ id: 'skill', component: 'switch', key: 'skill_evolution' }] }],
      }),
    /requires a module settings source/,
  );
  assert.throws(
    () =>
      create({
        id: 'agent',
        titleKey: 'settings.agent',
        icon: Icon,
        source: 'config',
        sections: [{ id: 'agent', items: [{ id: 'unknown', component: 'switch', key: 'unknown' }] }],
      }),
    /does not belong to source config/,
  );
  assert.throws(
    () =>
      create({
        id: 'browser',
        titleKey: 'settings.browser',
        icon: Icon,
        source: 'browser',
        sections: [{ id: 'browser', items: [{ id: 'type', component: 'select', key: 'browser_type', options: [] }] }],
      }),
    /has no options/,
  );
  assert.throws(
    () =>
      create({
        id: 'general',
        titleKey: 'settings.general',
        icon: Icon,
        source: 'unknown',
        sections: [{ id: 'general', items: [{ id: 'language', component: 'select', key: 'preferred_language' }] }],
      }),
    /unsupported settings source/,
  );
  assert.throws(
    () =>
      create({
        id: 'browser',
        titleKey: 'settings.browser',
        icon: Icon,
        source: 'browser',
        sections: [{ id: 'browser', items: [{ id: 'path', component: 'switch', key: 'chrome_path' }] }],
      }),
    /not compatible with browser setting chrome_path/,
  );
  assert.throws(
    () =>
      create({
        id: 'experimental',
        titleKey: 'settings.experimental',
        icon: Icon,
        source: 'config',
        sections: [
          {
            id: 'experimental',
            items: [
              {
                id: 'enabled',
                component: 'switch',
                key: 'proactive_recommendation_enabled',
                subItems: {
                  show: 'sometimes',
                  disabled: 'never',
                  items: [
                    {
                      id: 'limits',
                      component: 'custom',
                      render: Icon,
                    },
                  ],
                },
              },
            ],
          },
        ],
      }),
    /invalid subItems\.show/,
  );
  const definition = create({
    id: 'browser',
    titleKey: 'settings.browser',
    icon: Icon,
    source: 'browser',
    sections: [
      {
        id: 'browser',
        items: [
          {
            id: 'path',
            component: 'input',
            key: 'chrome_path',
          },
        ],
      },
    ],
  });
  const requiredKeys = new Set([
    'settings.browser',
    'settingsPanel.fields.chrome_path.title',
    'settingsPanel.fields.chrome_path.description',
    'settingsPanel.fields.chrome_path.placeholder',
  ]);
  validateSettingsI18n(definition, (key) => requiredKeys.has(key));
  requiredKeys.delete('settingsPanel.fields.chrome_path.placeholder');
  assert.throws(
    () => validateSettingsI18n(definition, (key) => requiredKeys.has(key)),
    /Missing settings i18n key: settingsPanel\.fields\.chrome_path\.placeholder/,
  );
});

test('open-source Settings composition preserves the registered modules and editable policy', () => {
  const Component = () => null;
  const base = createSettingsPageDefinition({
    id: 'open-source-settings',
    compositionMode: 'base',
    accessPolicy: { evaluate: () => ({ level: 'editable' }) },
    modules: [
      {
        id: 'general',
        titleKey: 'settings.general',
        icon: Component,
        sections: [{ id: 'general', items: [{ id: 'general-settings', component: 'custom', render: Component }] }],
      },
    ],
  });
  const composed = buildSettingsPageDefinition({
    id: 'open-source-settings',
    compositionMode: 'base',
    base,
    overlays: [],
  });
  assert.deepEqual(
    composed.modules.map((module) => module.id),
    base.modules.map((module) => module.id),
  );
  assert.equal(composed.compositionMode, 'base');
  assert.deepEqual(
    composed.accessPolicy.evaluate(
      { kind: 'module', moduleId: 'general' },
      { compositionMode: composed.compositionMode },
    ),
    { level: 'editable' },
  );
  assert.throws(
    () =>
      buildSettingsPageDefinition({
        id: 'invalid-open-source-settings',
        compositionMode: 'base',
        base,
        overlays: [{ id: 'not-allowed' }],
      }),
    /require extended composition/,
  );
});

test('KV Cache affinity remains registered but hidden until product release', () => {
  const context = { compositionMode: 'base' };
  assert.deepEqual(
    openSourceSettingsAccessPolicy.evaluate(
      { kind: 'section', moduleId: 'experimental', sectionId: 'kv-cache-affinity' },
      context,
    ),
    { level: 'hidden' },
  );
  assert.deepEqual(
    openSourceSettingsAccessPolicy.evaluate(
      { kind: 'section', moduleId: 'experimental', sectionId: 'trajectory-ui' },
      context,
    ),
    { level: 'editable' },
  );
});

test('extension Settings composition adds anchored modules and applies strict capability access', () => {
  const Component = () => null;
  const module = (id) => ({
    id,
    titleKey: `settings.${id}`,
    icon: Component,
    sections: [{ id, items: [{ id: `${id}-settings`, component: 'custom', render: Component }] }],
  });
  const base = createSettingsPageDefinition({
    id: 'base',
    compositionMode: 'base',
    accessPolicy: { evaluate: () => ({ level: 'editable' }) },
    modules: [module('general'), module('security')],
  });
  const extension = buildSettingsPageDefinition({
    id: 'extension',
    compositionMode: 'extended',
    base,
    overlays: [
      {
        id: 'extension-core',
        addModules: [
          {
            module: module('organization'),
            afterModuleId: 'general',
            access: { capability: 'settings.organization' },
          },
          {
            module: module('audit'),
            afterModuleId: 'general',
            access: { capability: 'settings.audit' },
          },
        ],
        accessBindings: [
          {
            target: { kind: 'module', moduleId: 'security' },
            capability: 'settings.security',
            reasonKey: 'extension.readOnly',
          },
        ],
        requiredI18nKeys: ['extension.readOnly'],
      },
    ],
    capabilitySnapshot: {
      revision: '42',
      capabilities: {
        'settings.organization': 'editable',
        'settings.audit': 'hidden',
        'settings.security': 'readOnly',
      },
    },
  });
  assert.deepEqual(
    extension.modules.map((candidate) => candidate.id),
    ['general', 'organization', 'audit', 'security'],
  );
  assert.deepEqual(
    extension.accessPolicy.evaluate({ kind: 'module', moduleId: 'organization' }, { compositionMode: 'extended' }),
    { level: 'editable' },
  );
  assert.deepEqual(
    extension.accessPolicy.evaluate({ kind: 'module', moduleId: 'audit' }, { compositionMode: 'extended' }),
    {
      level: 'hidden',
      reasonKey: undefined,
    },
  );
  assert.deepEqual(
    extension.accessPolicy.evaluate({ kind: 'module', moduleId: 'security' }, { compositionMode: 'extended' }),
    { level: 'readOnly', reasonKey: 'extension.readOnly' },
  );
  assert.deepEqual(
    restrictSettingsAccess(
      extension.accessPolicy.evaluate({ kind: 'module', moduleId: 'security' }, { compositionMode: 'extended' }),
      extension.accessPolicy.evaluate(
        { kind: 'item', moduleId: 'security', sectionId: 'security', itemId: 'security-settings' },
        { compositionMode: 'extended' },
      ),
    ),
    { level: 'readOnly', reasonKey: 'extension.readOnly' },
  );
  validateSettingsI18n(extension, (key) =>
    ['settings.general', 'settings.organization', 'settings.audit', 'settings.security', 'extension.readOnly'].includes(
      key,
    ),
  );
});

test('extension Settings composition rejects incomplete permissions and unauthorized replacements', () => {
  const Component = () => null;
  const Replacement = () => null;
  const base = createSettingsPageDefinition({
    id: 'base',
    compositionMode: 'base',
    accessPolicy: { evaluate: () => ({ level: 'editable' }) },
    modules: [
      {
        id: 'models',
        titleKey: 'settings.models',
        icon: Component,
        sections: [
          {
            id: 'models',
            items: [
              { id: 'replaceable-models', component: 'custom', render: Component, replaceable: true },
              { id: 'fixed-models', component: 'custom', render: Component },
            ],
          },
        ],
      },
    ],
  });
  assert.throws(
    () =>
      buildSettingsPageDefinition({
        id: 'extension',
        compositionMode: 'extended',
        base,
        overlays: [
          {
            id: 'extension',
            accessBindings: [{ target: { kind: 'module', moduleId: 'models' }, capability: 'settings.models' }],
          },
        ],
        capabilitySnapshot: { revision: '1', capabilities: {} },
      }),
    /missing capability: settings.models/,
  );
  assert.throws(
    () =>
      buildSettingsPageDefinition({
        id: 'extension',
        compositionMode: 'extended',
        base,
        overlays: [
          {
            id: 'extension',
            accessBindings: [{ target: { kind: 'module', moduleId: 'models' }, capability: 'settings.models' }],
          },
        ],
        capabilitySnapshot: { revision: '1', capabilities: { 'settings.models': 'owner' } },
      }),
    /invalid access level: owner/,
  );
  assert.throws(
    () =>
      buildSettingsPageDefinition({
        id: 'extension',
        compositionMode: 'extended',
        base,
        overlays: [],
        capabilitySnapshot: { revision: '1', capabilities: { 'settings.unbound': 'owner' } },
      }),
    /settings\.unbound has invalid access level: owner/,
  );
  assert.throws(
    () =>
      buildSettingsPageDefinition({
        id: 'extension',
        compositionMode: 'extended',
        base,
        overlays: [
          {
            id: 'extension',
            accessBindings: [{ target: { kind: 'module', moduleId: 'missing' }, capability: 'settings.missing' }],
          },
        ],
        capabilitySnapshot: { revision: '1', capabilities: { 'settings.missing': 'hidden' } },
      }),
    /access target does not exist: module:missing/,
  );
  assert.throws(
    () =>
      buildSettingsPageDefinition({
        id: 'extension',
        compositionMode: 'extended',
        base,
        overlays: [
          {
            id: 'extension',
            replaceItems: [
              {
                target: { kind: 'item', moduleId: 'models', sectionId: 'models', itemId: 'fixed-models' },
                item: { id: 'fixed-models', component: 'custom', render: Replacement },
              },
            ],
          },
        ],
        capabilitySnapshot: { revision: '1', capabilities: {} },
      }),
    /is not replaceable/,
  );
  const replaced = buildSettingsPageDefinition({
    id: 'extension',
    compositionMode: 'extended',
    base,
    overlays: [
      {
        id: 'extension',
        replaceItems: [
          {
            target: { kind: 'item', moduleId: 'models', sectionId: 'models', itemId: 'replaceable-models' },
            item: { id: 'replaceable-models', component: 'custom', render: Replacement },
          },
        ],
      },
    ],
    capabilitySnapshot: { revision: '1', capabilities: {} },
  });
  assert.equal(replaced.modules[0].sections[0].items[0].component, 'custom');
  assert.equal(replaced.modules[0].sections[0].items[0].render, Replacement);
});

test('new Settings architecture has no legacy giant page or setting-specific primitive layer', () => {
  assert.equal(existsSync(new URL('src/components/SettingsPanel/index.tsx', root)), false);
  assert.equal(existsSync(new URL('src/components/SettingsPanel/SettingsPrimitives.tsx', root)), false);
  const layout = source('src/features/settings/SettingsPageLayout.tsx');
  assert.doesNotMatch(layout, /case ['"](general|models|agent|browser|channels|security|experimental)/);
  assert.match(layout, /data-settings-module/);
  assert.match(layout, /settings-page__status/);
  assert.match(layout, /settingsPanel\.feedback\.(saving|saved|saveFailed)/);
  assert.doesNotMatch(layout, /SAVE_ERROR_TOAST_VISIBLE_MS|settings-page__toast/);
  assert.match(
    source('src/features/settings/services/SettingsSaveQueue.ts'),
    /Strictly serializes real settings writes/,
  );
  assert.match(source('src/components/form/core/FormStore.ts'), /deepEqual/);
  assert.doesNotMatch(source('src/components/form/core/FormStore.ts'), /JSON\.stringify/);
  assert.equal(
    source('src/features/settings/modules/channels/useSettingsChannelsController.ts').includes('webRequest'),
    false,
  );
  assert.equal(source('src/features/settings/modules/channels/useChannelForm.ts').includes('webRequest'), false);
});

test('simple Settings controls are declared per module and enforced by the shared renderer', () => {
  const modules = ['general', 'models', 'agent', 'browser', 'channels', 'experimental'];
  const definitions = modules.map((module) => ({
    module,
    source: source(`src/features/settings/modules/${module}/definition.ts`),
    file: parseTsx(`src/features/settings/modules/${module}/definition.ts`),
  }));
  const simpleDefinitions = definitions.flatMap(({ module, file }) =>
    findSettingDefinitions(file).map((definition) => ({ module, ...definition })),
  );

  for (const module of modules) {
    assert.match(
      source(`src/features/settings/modules/${module}/index.ts`),
      /export \{ \w+Module \} from '\.\/definition';/,
    );
  }
  assert.deepEqual([...new Set(simpleDefinitions.map(({ component }) => component))].sort(), [
    'input',
    'select',
    'switch',
  ]);
  for (const { module, key, component } of simpleDefinitions) {
    const titleKey = `settingsPanel.fields.${key}.title`;
    const descriptionKey = `settingsPanel.fields.${key}.description`;
    assert.equal(typeof translationAt(zh, titleKey), 'string', `${module}/${key} is missing Chinese title`);
    assert.equal(typeof translationAt(en, titleKey), 'string', `${module}/${key} is missing English title`);
    assert.equal(typeof translationAt(zh, descriptionKey), 'string', `${module}/${key} is missing Chinese description`);
    assert.equal(typeof translationAt(en, descriptionKey), 'string', `${module}/${key} is missing English description`);
    if (component === 'input') {
      assert.equal(typeof translationAt(zh, `settingsPanel.fields.${key}.placeholder`), 'string');
      assert.equal(typeof translationAt(en, `settingsPanel.fields.${key}.placeholder`), 'string');
    }
  }
  const types = source('src/features/settings/registry/types.ts');
  const renderer = source('src/features/settings/components/SettingItemRenderer.tsx');
  const sourceProvider = source('src/features/settings/services/SettingsSourceProvider.tsx');
  assert.match(types, /component: 'switch'/);
  assert.match(types, /component: 'select'/);
  assert.match(types, /component: 'input'/);
  assert.match(types, /component: 'custom'/);
  assert.match(types, /show: 'always' \| 'when-parent-checked'/);
  assert.match(types, /disabled: 'never' \| 'when-parent-unchecked'/);
  assert.match(renderer, /settingFieldKey\(item\.key, 'title'\)/);
  assert.match(renderer, /settingFieldKey\(item\.key, 'description'\)/);
  assert.match(sourceProvider, /source === 'config'/);
  assert.match(sourceProvider, /source === 'browser'/);
  assert.match(sourceProvider, /source === 'locale'/);
  assert.equal(existsSync(new URL('src/features/settings/modules/browser/BrowserSettings.tsx', root)), false);
  for (const file of sourceFilesUnder('src/features/settings/modules')) {
    assert.doesNotMatch(readFileSync(file, 'utf8'), /function ConfigBoolean/);
  }
});

test('shared Settings extension model remains product-neutral', () => {
  const forbiddenProductName = ['enter', 'prise'].join('');
  const files = [
    ...sourceFilesUnder('src/features/settings'),
    ...sourceFilesUnder('tests/fixtures/settings-extension'),
  ];
  for (const file of files) {
    assert.doesNotMatch(
      readFileSync(file, 'utf8'),
      new RegExp(forbiddenProductName, 'i'),
      `${file.pathname} contains product-specific naming`,
    );
  }
  assert.equal(existsSync(new URL(`src/features/settings/examples/${forbiddenProductName}/`, root)), false);
  assert.equal(existsSync(new URL(`${forbiddenProductName}-settings-demo.html`, root)), false);
});

test('Settings saved feedback clears two seconds after the latest successful write', async (context) => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  const queue = new SettingsSaveQueue();

  await queue.enqueue('first', async () => undefined);
  assert.equal(queue.getSnapshot().status, 'saved');
  context.mock.timers.tick(1999);
  assert.equal(queue.getSnapshot().status, 'saved');

  await queue.enqueue('second', async () => undefined);
  context.mock.timers.tick(1);
  assert.equal(queue.getSnapshot().status, 'saved');
  context.mock.timers.tick(1999);
  assert.equal(queue.getSnapshot().status, 'idle');
});

test('Settings save queue is strictly serial and continues after a rejected write', async () => {
  const queue = new SettingsSaveQueue();
  const order = [];
  const first = queue.enqueue('first', async () => {
    order.push('first:start');
    await Promise.resolve();
    order.push('first:end');
    return 1;
  });
  const rejected = queue.enqueue('bad', async () => {
    order.push('bad');
    throw new Error('bad');
  });
  const last = queue.enqueue('last', async () => {
    order.push('last');
    return 3;
  });
  assert.equal(await first, 1);
  await assert.rejects(rejected, /bad/);
  assert.equal(await last, 3);
  assert.deepEqual(order, ['first:start', 'first:end', 'bad', 'last']);
  queue.clear();
});

test('Settings save queue exposes failed writes for the persistent page-level error status', async () => {
  const queue = new SettingsSaveQueue();

  await assert.rejects(
    queue.enqueue('save.settings', async () => {
      throw new Error('write failed');
    }),
    /write failed/,
  );

  assert.deepEqual(queue.getSnapshot(), {
    status: 'error',
    operation: 'save.settings',
    error: 'write failed',
  });
});

test('Settings save queue keeps caller-scoped failures out of the page-level status', async () => {
  const queue = new SettingsSaveQueue();

  await assert.rejects(
    queue.enqueue(
      'model.add',
      async () => {
        throw new Error('dialog save failed');
      },
      { errorScope: 'caller' },
    ),
    /dialog save failed/,
  );

  assert.deepEqual(queue.getSnapshot(), {
    status: 'idle',
    operation: null,
    error: null,
  });
});

test('Settings request router uses exact method ownership and rejects ambiguous or unknown methods', async () => {
  const calls = [];
  const openSourceRequest = async (method) => {
    calls.push(`oss:${method}`);
    return { source: 'oss' };
  };
  const extensionRequest = async (method) => {
    calls.push(`extension:${method}`);
    return { source: 'extension' };
  };
  const request = createSettingsRequestRouter([
    { id: 'open-source', methods: ['config.get'], request: openSourceRequest },
    { id: 'extension', methods: ['sample.organization.get'], request: extensionRequest },
  ]);
  assert.deepEqual(await request('config.get'), { source: 'oss' });
  assert.deepEqual(await request('sample.organization.get'), { source: 'extension' });
  assert.deepEqual(calls, ['oss:config.get', 'extension:sample.organization.get']);
  await assert.rejects(request('config.unknown'), /No settings request route registered/);
  assert.throws(
    () =>
      createSettingsRequestRouter([
        { id: 'open-source', methods: ['config.get'], request: openSourceRequest },
        { id: 'extension', methods: ['config.get'], request: extensionRequest },
      ]),
    /Duplicate settings request method: config.get/,
  );
});

test('settings contract rejects unknown keys and normalizes supported values', () => {
  assert.equal(new Set(SETTINGS_CONFIG_FIELDS.map((field) => field.key)).size, SETTINGS_CONFIG_FIELDS.length);
  assert.equal(
    SETTINGS_CONFIG_FIELDS.some((field) => field.key === 'permissions_mode'),
    false,
  );
  assert.equal(
    SETTINGS_CONFIG_FIELDS.some((field) => field.key === 'permissions_enabled'),
    true,
  );
  assert.throws(() => buildConfigSavePayload({ unknown: 'x' }), /Unknown settings config key/);
  assert.deepEqual(
    normalizeSettingsConfigUpdates({
      external_cli_agent_claude_cli_path: '  /usr/bin/claude  ',
      proactive_recommendation_max_recommend_per_day: ' 10 ',
      permissions_enabled: 'true',
    }),
    {
      external_cli_agent_claude_cli_path: '/usr/bin/claude',
      proactive_recommendation_max_recommend_per_day: '10',
      permissions_enabled: 'true',
    },
  );
  assert.throws(() => normalizeSettingsConfigUpdates({ unknown: 'x' }), /Unknown settings config key/);
  assert.deepEqual(buildModelsSavePayload([{ model_name: 'test' }]), { models: [{ model_name: 'test' }] });
  assert.deepEqual(
    buildModelValidationPayload({
      model_name: 'm',
      api_base: 'https://example.test',
      api_key: 'k',
      model_provider: 'OpenAI',
      endpoint_profile: 'openrouter',
    }),
    {
      api_base: 'https://example.test',
      api_key: 'k',
      model: 'm',
      model_provider: 'OpenAI',
      reasoning_level: undefined,
      endpoint_profile: 'openrouter',
    },
  );
  assert.equal(normalizePermissionLevel(' ASK '), 'ask');
});

test('Settings i18n is symmetrical and includes the optional field affordance', () => {
  const zhKeys = leafKeys(zh.settingsPanel).sort();
  const enKeys = leafKeys(en.settingsPanel).sort();
  assert.deepEqual(enKeys, zhKeys);
  assert.deepEqual(leafKeys(en.channels).sort(), leafKeys(zh.channels).sort());
  assert.equal(zh.common.optional, '（可选）');
  assert.equal(en.common.optional, '(optional)');
  assert.equal(zh.channels.xiaoyiApps.defaultAppName, '默认小艺应用');
  assert.equal(en.channels.xiaoyiApps.defaultAppName, 'Default Xiaoyi App');
  assert.doesNotMatch(source('src/features/settings/modules/channels/channelAdapters.ts'), /默认小艺应用|zh-Hans-CN/);
  const dynamicAgentKeys = [
    ...mediaCapabilityModalities.flatMap((modality) => [
      `settingsPanel.agent.${modality}`,
      `settingsPanel.agent.${modality}Description`,
      `settingsPanel.agent.${modality}ConfigTitle`,
    ]),
    'settingsPanel.agent.toggleCapability',
    'settingsPanel.agent.saveAndEnable',
    'settingsPanel.agent.savedRestartRequired',
  ];
  for (const key of dynamicAgentKeys) {
    assert.equal(typeof translationAt(zh, key), 'string', `Missing Chinese translation ${key}`);
    assert.equal(typeof translationAt(en, key), 'string', `Missing English translation ${key}`);
  }
  for (const file of sourceFilesUnder('src/features/settings').filter((candidate) =>
    /\.tsx?$/.test(candidate.pathname),
  )) {
    const parsed = ts.createSourceFile(
      file.pathname,
      readFileSync(file, 'utf8'),
      ts.ScriptTarget.Latest,
      true,
      ts.ScriptKind.TSX,
    );
    for (const key of findLiteralTranslationKeys(parsed)) {
      assert.equal(typeof translationAt(zh, key), 'string', `${file.pathname} is missing Chinese translation ${key}`);
      assert.equal(typeof translationAt(en, key), 'string', `${file.pathname} is missing English translation ${key}`);
    }
  }
});

test('every visible Settings control maps to an exact persistence field or RPC', () => {
  const contractByCategory = (category) =>
    new Set(SETTINGS_CONFIG_FIELDS.filter((field) => field.category === category).map((field) => field.key));
  const agentFile = parseTsx('src/features/settings/modules/agent/AgentSettings.tsx');
  const agentDefinition = parseTsx('src/features/settings/modules/agent/definition.ts');
  const agentVisible = new Set([
    ...findSettingDefinitionKeys(agentDefinition),
    ...findVariableArrayStrings(agentFile, 'keyFields'),
    ...findVariableArrayStrings(agentFile, 'modalities').flatMap((modality) => [
      ...['api_base', 'api_key', 'model', 'provider', 'endpoint_profile', 'vendor_key', 'plan'].map(
        (suffix) => `${modality}_${suffix}`,
      ),
      `${modality}_enabled`,
    ]),
  ]);
  assert.deepEqual(
    [...agentVisible].sort(),
    [...contractByCategory('agent')].filter((key) => key !== 'github_token').sort(),
  );

  const modelVisible = new Set(
    findSettingDefinitionKeys(parseTsx('src/features/settings/modules/models/definition.ts')),
  );
  assert.deepEqual(
    [...modelVisible],
    [...contractByCategory('models')].filter((key) => !key.startsWith('embed_')),
  );
  assert.deepEqual(findSettingDefinitionKeys(parseTsx('src/features/settings/modules/experimental/definition.ts')), [
    'kv_cache_affinity_enabled',
    'proactive_recommendation_enabled',
  ]);
  assert.deepEqual([...contractByCategory('experimental')].sort(), [
    'a2ui_enabled',
    'external_cli_agent_claude_cli_path',
    'external_cli_agent_claude_enabled',
    'external_cli_agent_claude_use_builtin',
    'external_cli_agent_codex_cli_path',
    'external_cli_agent_codex_enabled',
    'external_cli_agent_codex_use_builtin',
    'kv_cache_affinity_enabled',
    'proactive_recommendation_enabled',
    'proactive_recommendation_max_recommend_per_day',
    'proactive_recommendation_max_rounds_per_tick',
    'trajectory_ui_enabled',
  ]);

  const channelCatalogFile = parseTsx('src/features/settings/modules/channels/channelCatalog.ts');
  assert.deepEqual(findVariableArrayStrings(channelCatalogFile, 'SETTINGS_CHANNEL_IDS'), [
    'xiaoyi',
    'feishu',
    'dingtalk',
    'telegram',
    'discord',
    'slack',
    'whatsapp',
  ]);
  const channelAdaptersFile = parseTsx('src/features/settings/modules/channels/channelAdapters.ts');
  const channelPayloadKeys = {
    buildXiaoyiApp: ['enabled', 'ak', 'sk', 'agent_id', 'api_id', 'enable_streaming', 'name', 'is_default'],
    buildFeishuApp: [
      'enabled',
      'enable_streaming',
      'app_id',
      'app_secret',
      'encrypt_key',
      'verification_token',
      'chat_id',
      'allow_from',
      'group_digital_avatar',
      'my_user_id',
      'bot_name',
      'enable_memory',
      'name',
      'is_default',
    ],
    buildDingtalkFormPayload: ['enabled', 'client_id', 'client_secret', 'allow_from'],
    buildTelegramFormPayload: ['enabled', 'bot_token', 'allow_from', 'parse_mode', 'group_chat_mode'],
    buildDiscordFormPayload: [
      'enabled',
      'bot_token',
      'application_id',
      'guild_id',
      'channel_id',
      'block_dm',
      'allow_from',
    ],
    buildSlackFormPayload: [
      'enabled',
      'bot_token',
      'app_token',
      'allow_from',
      'allowed_channel_ids',
      'default_channel_id',
      'reply_in_thread',
    ],
    buildWhatsAppFormPayload: [
      'enabled',
      'bridge_ws_url',
      'default_jid',
      'allow_from',
      'enable_streaming',
      'auto_start_bridge',
      'bridge_command',
      'bridge_workdir',
    ],
  };
  for (const [functionName, expectedKeys] of Object.entries(channelPayloadKeys)) {
    assert.deepEqual(findReturnedObjectKeys(channelAdaptersFile, functionName), expectedKeys);
  }

  const settingsConfigMethods = findRequestMethods(parseTsx('src/features/settings/services/useSettingsConfig.ts'));
  assert.deepEqual([...settingsConfigMethods].sort(), ['config.get', 'config.save_all']);
  const sourceMethods = findRequestMethods(parseTsx('src/features/settings/services/SettingsSourceProvider.tsx'));
  assert.deepEqual([...sourceMethods].sort(), ['locale.set_conf', 'path.get', 'path.set']);
  const channelController = source('src/features/settings/modules/channels/useSettingsChannelsController.ts');
  const channelFormHook = source('src/features/settings/modules/channels/useChannelForm.ts');
  assert.match(channelFormHook, /request<\{ config\?: unknown \}>\(getMethod\)/);
  assert.match(channelFormHook, /request<\{ config\?: unknown \}>\(setMethod, payload\)/);
  for (const channelId of ['xiaoyi', 'feishu', 'dingtalk', 'telegram', 'discord', 'slack', 'whatsapp']) {
    assert.match(channelController, new RegExp(`getMethod: 'channel\\.${channelId}\\.get_conf'`));
    assert.match(channelController, new RegExp(`setMethod: 'channel\\.${channelId}\\.set_conf'`));
  }
});

test('saving the free-model switch refreshes the shared model catalog after persistence', () => {
  const settingsConfig = source('src/features/settings/services/useSettingsConfig.ts');
  const settingsPage = source('src/features/settings/SettingsPage.tsx');
  const settingsServices = source('src/features/settings/services/SettingsServicesProvider.tsx');
  const app = source('src/App.tsx');
  assert.match(settingsServices, /onConfigSaved\?: \(updatedKeys: readonly string\[\]\) => Promise<void> \| void/);
  assert.match(settingsConfig, /setConfig\([\s\S]{0,120}await onConfigSaved\?\.\(Object\.keys\(updates\)\)/);
  assert.match(settingsPage, /onConfigSaved=\{onConfigSaved\}/);
  assert.match(app, /updatedKeys\.includes\('enable_free_models'\)\) await handleModelsRefresh\(\)/);
  assert.match(app, /onConfigSaved=\{handleSettingsConfigSaved\}/);
});

test('Settings form dialogs share the same dirty-close contract without disabling save', () => {
  const closeHook = source('src/features/settings/services/useSettingsFormDialogClose.ts');
  assert.match(closeHook, /const \{ hasUnsavedChanges \} = useFormState\(form\)/);
  assert.match(closeHook, /useUnsavedChanges\(id, hasUnsavedChanges\)/);
  assert.match(closeHook, /function requestClose\(\): void \{[\s\S]{0,220}hasUnsavedChanges[\s\S]{0,160}onClose\(\)/);
  assert.match(closeHook, /function cancelDiscard\(\): void \{\s*setDiscardConfirmationOpen\(false\)/);
  assert.match(
    closeHook,
    /function confirmDiscard\(\): void \{\s*form\.reset\(\);\s*setDiscardConfirmationOpen\(false\);\s*onClose\(\)/,
  );

  for (const { path, id } of [
    { path: 'src/features/settings/modules/agent/AgentSettings.tsx', id: 'agent-config-dialog' },
    { path: 'src/features/settings/modules/experimental/ExperimentalSettings.tsx', id: 'proactive-limits-dialog' },
    { path: 'src/features/settings/modules/models/ModelDialog.tsx', id: 'model-dialog' },
  ]) {
    const module = source(path);
    assert.doesNotMatch(module, /confirmDisabled=\{!hasUnsavedChanges\}/);
    assert.match(
      module,
      new RegExp(`useSettingsFormDialogClose\\(\\{[\\s\\S]{0,120}id: '${id}'[\\s\\S]{0,180}onClose`),
    );
    assert.doesNotMatch(module, /setDiscardConfirmationOpen/);
    assert.match(module, /<FormDialog[\s\S]{0,900}onCancel=\{requestClose\}/);
    assert.match(
      module,
      /<SettingsConfirmDialog\s+open=\{discardConfirmationOpen\}[\s\S]{0,260}onConfirm=\{confirmDiscard\}\s+onCancel=\{cancelDiscard\}/,
    );
  }

  const channelDialog = source('src/features/settings/modules/channels/components/ChannelConfigDialog.tsx');
  const channelFormHook = source('src/features/settings/modules/channels/useChannelForm.ts');
  const channelController = source('src/features/settings/modules/channels/useSettingsChannelsController.ts');
  assert.match(channelDialog, /confirmDisabled=\{!isConnected\}/);
  assert.doesNotMatch(channelDialog, /confirmDisabled=\{[^}]*hasUnsavedChanges/);
  assert.match(
    channelFormHook,
    /const save = useCallback\(async \(\) => \{\s*const validation = form\.validate\(\);[\s\S]*return persistPayload\(buildPayload\(validation\.values\), savedMessage\)/s,
  );
  assert.match(channelController, /feishu\.form\.setFieldValue\('apps', \[\.\.\.apps, nextApp\]\)/);
  assert.match(channelController, /setActiveDialogBaseline\(feishu\.form\.getValues\(\)\)/);
  assert.match(
    channelController,
    /const hasActiveDialogChanges = activeDialogBaseline\s*\? !deepEqual\(activeController\.form\.getValues\(\), activeDialogBaseline\)\s*: activeController\.hasUnsavedChanges/,
  );
  assert.match(
    channelController,
    /function resetActiveDialog\(\): void \{\s*activeController\.reset\(\);\s*setActiveDialogBaseline\(null\)/,
  );
  assert.match(
    channelController,
    /const closeDialog = \(\) => \{[\s\S]{0,240}resetActiveDialog\(\);\s*setDialogOpen\(false\)/,
  );
});

test('Agent configuration entry points are disabled while the backend is connecting', () => {
  const agentSettings = source('src/features/settings/modules/agent/AgentSettings.tsx');
  assert.equal(agentSettings.match(/disabled=\{disabled \|\| !isConnected\}/g)?.length, 1);
  assert.equal(agentSettings.match(/disabled=\{disabled \|\| !isConnected \|\| busy\}/g)?.length, 2);
  assert.match(agentSettings, /<FormDialog[\s\S]*confirmDisabled=\{!isConnected\}/);
});

test('media capability configuration and hot-apply state use exact fields', () => {
  const agentSettings = source('src/features/settings/modules/agent/AgentSettings.tsx');
  const values = {
    vision_api_base: 'https://vision.example/v1',
    vision_api_key: 'secret',
    vision_model: 'vision-1',
    vision_provider: 'OpenAI',
  };
  assert.deepEqual(mediaCapabilityConfigFields('vision'), [
    'vision_api_base',
    'vision_api_key',
    'vision_model',
    'vision_provider',
  ]);
  assert.equal(mediaCapabilityEnabledField('vision'), 'vision_enabled');
  assert.deepEqual(mediaCapabilityPersistenceFields('vision'), [
    'vision_api_base',
    'vision_api_key',
    'vision_model',
    'vision_provider',
    'vision_endpoint_profile',
    'vision_vendor_key',
    'vision_plan',
  ]);
  assert.deepEqual([...mediaCapabilityModalities], ['vision', 'audio', 'video', 'image_gen']);
  assert.equal(mediaCapabilityEnabledField('image_gen'), 'image_gen_enabled');
  assert.deepEqual(mediaCapabilityPersistenceFields('image_gen'), [
    'image_gen_api_base',
    'image_gen_api_key',
    'image_gen_model',
    'image_gen_provider',
    'image_gen_endpoint_profile',
    'image_gen_vendor_key',
    'image_gen_plan',
  ]);
  assert.equal(isMediaCapabilityConfigured(values, 'vision'), true);
  assert.equal(isMediaCapabilityConfigured({ ...values, vision_provider: '  ' }, 'vision'), false);
  assert.equal(wasConfigAppliedWithoutRestart({ applied_without_restart: true }), true);
  assert.equal(wasConfigAppliedWithoutRestart({ applied_without_restart: false }), false);
  assert.equal(wasConfigAppliedWithoutRestart({}), false);
  assert.doesNotMatch(agentSettings, /settingsActionIcons\.delete/);
});

test('search dialogs keep the shared required-field contract', () => {
  const agentSettings = source('src/features/settings/modules/agent/AgentSettings.tsx');
  const agentSettingsFile = parseTsx('src/features/settings/modules/agent/AgentSettings.tsx');
  assert.deepEqual(findVariableArrayStrings(agentSettingsFile, 'keyFields'), [
    'jina_api_key',
    'bocha_api_key',
    'perplexity_api_key',
    'serper_api_key',
  ]);
  assert.match(agentSettings, /const required = isRequiredAgentConfigField\(name\)/);
  assert.match(agentSettings, /required[,}]/);
  assert.match(agentSettings, /fields\.filter\(isRequiredAgentConfigField\)/);
  assert.match(agentSettings, /String\(value \?\? ''\)\.trim\(\)/);
  assert.match(agentSettings, /<Form form=\{form\} items=\{items\} rules=\{rules\}/);
});

test('multimodal dialogs reuse provider-first model configuration without model testing or account login', () => {
  const agentSettings = source('src/features/settings/modules/agent/AgentSettings.tsx');
  const dialog = source('src/features/settings/modules/agent/MediaModelConfigDialog.tsx');
  assert.match(agentSettings, /<MediaModelConfigDialog/);
  assert.match(dialog, /<ModelProviderSelect/);
  assert.match(dialog, /includeOpenAIAccount=\{false\}/);
  assert.match(dialog, /<ModelNameField/);
  assert.match(dialog, /'vendors\.list'/);
  assert.match(dialog, /'vendors\.fetch_models'/);
  assert.match(dialog, /showOptional=\{false\}/);
  assert.doesNotMatch(dialog, /config\.validate_model|OpenAIAccountSettings|reasoning_level|settingsActionIcons\.delete/);
});

test('legacy multimodal configuration remains custom while provider selections persist exact catalog identity', () => {
  const legacy = {
    vision_api_base: 'https://legacy.example/v1',
    vision_api_key: 'legacy-key',
    vision_model: 'legacy-model',
    vision_provider: 'OpenAI',
  };
  const legacyDraft = createMediaModelDraft(legacy, 'vision');
  assert.equal(legacyDraft.vendor_selection, 'custom');
  assert.equal(legacyDraft.model_input_mode, 'manual');
  assert.equal(legacyDraft.api_base, legacy.vision_api_base);
  assert.equal(legacyDraft.api_key, legacy.vision_api_key);
  assert.equal(legacyDraft.model_name, legacy.vision_model);
  assert.equal(legacyDraft.provider, legacy.vision_provider);

  const preset = {
    vendor_key: 'example',
    display_name: 'Example',
    plan: 'token_plan',
    client_provider: 'OpenAI',
    api_base: 'https://preset.example/v1',
    endpoint_profile: 'example-profile',
    default_model: 'example-vision',
    model_options: ['example-vision'],
    icon_key: 'example',
    models_endpoint: '/models',
    models_needs_key: true,
    supports_anthropic: false,
    anthropic_base: null,
    anthropic_client_provider: null,
  };
  const catalog = { token_plan: [preset], coding_plan: [], custom_api: [] };
  const updates = buildMediaModelConfigUpdates(
    {
      ...legacyDraft,
      vendor_selection: 'token_plan:example',
      model_input_mode: 'options',
      api_key: 'new-key',
      model_name: 'example-vision',
    },
    catalog,
    'vision',
    true,
  );
  assert.deepEqual(updates, {
    vision_api_base: 'https://preset.example/v1',
    vision_api_key: 'new-key',
    vision_model: 'example-vision',
    vision_provider: 'OpenAI',
    vision_endpoint_profile: 'example-profile',
    vision_vendor_key: 'example',
    vision_plan: 'token_plan',
    vision_enabled: 'true',
  });
  const editedDraft = createMediaModelDraft(updates, 'vision');
  assert.equal(editedDraft.vendor_selection, 'token_plan:example');
  assert.equal(editedDraft.model_input_mode, 'options');
  assert.equal(editedDraft.vendor_key, 'example');
  assert.equal(editedDraft.plan, 'token_plan');
});

test('SettingRow exposes a business-agnostic subSettings slot for dependent rows', () => {
  const settingRow = source('src/features/settings/components/SettingRow.tsx');
  const settingRowCss = source('src/features/settings/components/SettingRow.css');
  const itemRenderer = source('src/features/settings/components/SettingItemRenderer.tsx');
  const collapsibleText = source('src/components/ui/CollapsibleText/CollapsibleText.tsx');
  const collapsibleTextCss = source('src/components/ui/CollapsibleText/CollapsibleText.css');
  const uiComponents = source('src/components/ui/index.ts');
  const settingsComponents = source('src/features/settings/components/index.ts');
  const experimentalDefinition = source('src/features/settings/modules/experimental/definition.ts');
  const browserDefinition = source('src/features/settings/modules/browser/definition.ts');
  const sourceProvider = source('src/features/settings/services/SettingsSourceProvider.tsx');
  const sourceContract = source('src/features/settings/services/settingsSourceContract.ts');
  const settingsContract = source('src/features/settings/services/settingsContract.ts');

  assert.match(settingRow, /subSettings\?: ReactNode/);
  assert.match(settingRow, /controlPlacement\?: 'center' \| 'top'/);
  assert.match(settingRow, /className="setting-row__sub-settings">\{subSettings\}/);
  assert.doesNotMatch(settingRow, /memory|forbidden|enabled|config|save/i);
  assert.match(settingRowCss, /\.setting-row__main--control-top/);
  assert.match(settingRowCss, /\.setting-row__sub-settings::before/);
  assert.match(settingRowCss, /right: 16px;[\s\S]*left: 16px;/);
  assert.match(settingRowCss, /\.setting-row__sub-settings > \.setting-row/);
  assert.match(collapsibleText, /content\.scrollHeight > lineHeight \* maxLines/);
  assert.match(collapsibleText, /new ResizeObserver\(updateOverflow\)/);
  assert.doesNotMatch(collapsibleText, /memory|forbidden|config|save/i);
  assert.match(collapsibleTextCss, /color: inherit/);
  assert.doesNotMatch(collapsibleTextCss, /--color-settings-/);
  assert.match(uiComponents, /export \{ CollapsibleText, type CollapsibleTextProps \}/);
  assert.doesNotMatch(settingsComponents, /CollapsibleText/);
  assert.match(itemRenderer, /subSettings=\{[\s\S]*item\.subItems\.items\.map/);
  assert.match(itemRenderer, /item\.subItems\?\.show === 'always' \|\| checked/);
  assert.match(itemRenderer, /item\.subItems\?\.disabled === 'when-parent-unchecked'/);
  assert.match(experimentalDefinition, /show: 'always',[\s\S]*disabled: 'when-parent-unchecked'/);
  assert.doesNotMatch(browserDefinition, /component: 'switch'|key: 'enabled'|subItems:/);
  assert.deepEqual(findSettingDefinitionKeys(parseTsx('src/features/settings/modules/browser/definition.ts')), [
    'chrome_path',
    'browser_type',
    'headless',
  ]);
  assert.match(browserDefinition, /\{ value: false, labelKey: 'settingsPanel\.browser\.headed' \}/);
  assert.match(browserDefinition, /\{ value: true, labelKey: 'settingsPanel\.browser\.headless' \}/);
  assert.match(browserDefinition, /\{ value: 'auto', labelKey: 'browser\.browserTypeAuto' \}/);
  assert.match(browserDefinition, /\{ value: 'chrome', labelKey: 'browser\.browserTypeChrome' \}/);
  assert.match(browserDefinition, /\{ value: 'msedge', labelKey: 'browser\.browserTypeEdge' \}/);
  assert.match(sourceProvider, /request<Record<string, unknown>>\('path\.get'\)/);
  assert.match(sourceProvider, /request<Record<string, unknown>>\('path\.set', next/);
  assert.doesNotMatch(sourceProvider, /enabled|onlyEnabled/);
  assert.doesNotMatch(sourceContract, /\['enabled', 'switch'\]/);
  assert.doesNotMatch(settingsContract, /react\.subagents\.browser_agent\.enabled/);
  assert.equal(zh.settingsPanel.browser.enabled, undefined);
  assert.equal(en.settingsPanel.browser.enabled, undefined);
  assert.equal(zh.settingsPanel.fields.enabled, undefined);
  assert.equal(en.settingsPanel.fields.enabled, undefined);
  assert.match(itemRenderer, /readOnly=\{!editing\}/);
  assert.match(itemRenderer, /editing \? \([\s\S]*common\.cancel[\s\S]*common\.save/);
  assert.match(itemRenderer, /common\.modify/);
});

test('Settings tags use the shared UI Tag component and semantic variants', () => {
  const settingsPageCss = source('src/features/settings/SettingsPage.css');
  const tagSource = source('src/components/ui/Tag/Tag.tsx');
  const tagCss = source('src/components/ui/Tag/Tag.css');
  const uiIndex = source('src/components/ui/index.ts');
  const lightTheme = source('src/styles/themes/default/light.css');
  const generalSettings = source('src/features/settings/modules/general/GeneralSettings.tsx');
  const modelsSettings = source('src/features/settings/modules/models/ModelsSettings.tsx');
  const settingRow = source('src/features/settings/components/SettingRow.tsx');

  assert.match(generalSettings, /<Tag\s+variant=\{connectionVariant\}\s+role="status">/s);
  assert.match(generalSettings, /const connectionVariant:\s*TagVariant/);
  assert.match(modelsSettings, /<Tag\s+variant="info">\{t\('settingsPanel\.models\.primary'\)\}<\/Tag>/);
  assert.match(modelsSettings, /<Tag\s+variant="neutral">\{t\('settingsPanel\.models\.groupDefault'\)\}<\/Tag>/);
  assert.match(modelsSettings, /<Tag\s+variant="neutral">\{t\('settingsPanel\.models\.agentOsReadonly'\)\}<\/Tag>/);
  assert.match(uiIndex, /export \{ Tag, type TagProps, type TagVariant \} from '\.\/Tag\/Tag'/);
  assert.match(tagSource, /export type TagVariant = 'success' \| 'info' \| 'warning' \| 'danger' \| 'neutral'/);
  assert.match(
    tagCss,
    /\.ui-tag\s*\{[^}]*display:\s*inline-flex[^}]*justify-content:\s*center[^}]*width:\s*max-content[^}]*height:\s*18px[^}]*padding:\s*0 8px[^}]*font-size:\s*12px[^}]*font-weight:\s*400[^}]*line-height:\s*18px[^}]*border-radius:\s*2px/s,
  );
  assert.match(
    tagCss,
    /\.ui-tag--success\s*\{[^}]*var\(--color-feedback-success-text\)[^}]*var\(--color-feedback-success-toast\)/s,
  );
  assert.match(tagCss, /\.ui-tag--info\s*\{[^}]*var\(--color-tag-info-text\)[^}]*var\(--color-tag-info-surface\)/s);
  assert.match(
    tagCss,
    /\.ui-tag--warning\s*\{[^}]*var\(--color-feedback-warning\)[^}]*var\(--color-feedback-warning-subtle\)/s,
  );
  assert.match(
    tagCss,
    /\.ui-tag--danger\s*\{[^}]*var\(--color-feedback-danger\)[^}]*var\(--color-feedback-danger-subtle\)/s,
  );
  assert.match(tagCss, /\.ui-tag--neutral\s*\{[^}]*var\(--color-tag-neutral-text\)[^}]*var\(--color-tag-neutral-surface\)/s);
  assert.match(modelsSettings, /className="settings-model-card__text-action"/);
  assert.match(
    settingsPageCss,
    /\.settings-model-card__actions \.settings-model-card__text-action\s*\{[^}]*color:\s*var\(--color-settings-link\)/s,
  );
  assert.doesNotMatch(
    settingsPageCss,
    /settings-page__badge|settings-general__connection|settings-model-card__group-default|settings-model-card__readonly/,
  );
  assert.match(settingRow, /children !== undefined && children !== null/);
  assert.match(lightTheme, /--color-feedback-success:\s*#16a34a;/i);
  assert.match(lightTheme, /--color-feedback-success-text:\s*#01802b;/i);
  assert.match(lightTheme, /--color-feedback-success-toast:\s*#d5f2dc;/i);
  assert.match(lightTheme, /--color-feedback-info:\s*#2563eb;/i);
  assert.match(lightTheme, /--color-feedback-info-subtle:\s*rgba\(59, 130, 246, 0\.08\);/i);
  assert.match(lightTheme, /--color-tag-info-text:\s*#0f5ed4;/i);
  assert.match(lightTheme, /--color-tag-info-surface:\s*#deecff;/i);
  assert.match(lightTheme, /--color-tag-neutral-text:\s*#191919;/i);
  assert.match(lightTheme, /--color-tag-neutral-surface:\s*#f5f5f5;/i);
});

test('Settings high-fidelity visual contract remains wired to exact assets and spacing', () => {
  const settingsPageCss = source('src/features/settings/SettingsPage.css');
  const settingsPageLayout = source('src/features/settings/SettingsPageLayout.tsx');
  const registryTypes = source('src/features/settings/registry/types.ts');
  const buttonCss = source('src/components/ui/Button/Button.css');
  const lightTheme = source('src/styles/themes/default/light.css');
  const settingsSection = source('src/features/settings/components/SettingsSection.tsx');
  const settingsSectionCss = source('src/features/settings/components/SettingsSection.css');
  const settingRowCss = source('src/features/settings/components/SettingRow.css');
  const formCss = source('src/components/form/components/Form.css');
  const formDialog = source('src/components/form/components/FormDialog.tsx');
  const formDialogCss = source('src/components/form/components/FormDialog.css');
  const settingsConfirmDialog = source('src/features/settings/components/SettingsConfirmDialog.tsx');
  const settingsConfirmDialogCss = source('src/features/settings/components/SettingsConfirmDialog.css');
  const channelsCss = source('src/features/settings/modules/channels/SettingsChannelsPanel.css');
  const channelsPanel = source('src/features/settings/modules/channels/SettingsChannelsPanel.tsx');
  const channelsModule = source('src/features/settings/modules/channels/index.ts');
  const channelsDefinition = source('src/features/settings/modules/channels/definition.ts');
  const channelList = source('src/features/settings/modules/channels/components/ChannelListSection.tsx');
  const channelDialog = source('src/features/settings/modules/channels/components/ChannelConfigDialog.tsx');
  const feishuForm = source('src/features/settings/modules/channels/forms/FeishuChannelForm.tsx');
  const standardChannelForm = source('src/features/settings/modules/channels/forms/StandardChannelForm.tsx');
  const channelFormItems = source('src/features/settings/modules/channels/channelFormItems.ts');
  const channelRequirements = source('src/features/settings/modules/channels/channelRequirements.ts');
  const modelsSettings = source('src/features/settings/modules/models/ModelsSettings.tsx');
  const modelsDefinition = source('src/features/settings/modules/models/definition.ts');
  const modelDialog = source('src/features/settings/modules/models/ModelDialog.tsx');
  const modelProviderSelect = source('src/features/settings/modules/models/ModelProviderSelect.tsx');
  const modelProviderIcon = source('src/components/ModelProviderIcon/index.tsx');
  const providerAssets = source('src/assets/providers/index.ts');
  const settingsAssets = source('src/assets/settings/index.ts');
  const generalDefinition = source('src/features/settings/modules/general/definition.ts');
  const sidebar = source('src/components/SessionSidebar/index.tsx');
  const appSettingsIcon = source('src/assets/settings/app-navigation/settings.svg');

  assert.match(settingsPageCss, /grid-template-columns:\s*clamp\(208px,\s*15\.4167vw,\s*296px\)/);
  assert.match(settingsPageCss, /\.settings-page__nav\s*\{[^}]*padding:\s*20px 16px 36px/s);
  assert.match(
    settingsPageCss,
    /\.settings-page__nav > h1\s*\{[^}]*padding:\s*0 0 0 8px[^}]*margin-bottom:\s*20px[^}]*font-size:\s*16px[^}]*line-height:\s*24px[^}]*letter-spacing:\s*0/s,
  );
  assert.match(
    settingsPageCss,
    /\.settings-page__nav-button\s*\{[^}]*height:\s*32px[^}]*gap:\s*8px[^}]*padding:\s*5px 8px[^}]*font-size:\s*14px[^}]*font-weight:\s*400[^}]*line-height:\s*22px[^}]*letter-spacing:\s*0/s,
  );
  assert.match(settingsPageCss, /width:\s*min\(1104px,\s*100%\)/);
  assert.match(settingsPageCss, /\.settings-page__item\s*\{[^}]*display:\s*grid[^}]*gap:\s*16px/s);
  assert.match(settingsPageLayout, /separatedRows=\{section\.separatedRows === true\}/);
  assert.match(registryTypes, /separatedRows\?: boolean/);
  assert.doesNotMatch(registryTypes, /groupedRows/);
  assert.match(settingsSection, /separatedRows = false/);
  assert.match(settingsSection, /separatedRows \? '' : ' settings-section__items--grouped'/);
  assert.match(
    settingsSectionCss,
    /\.settings-section__items--grouped\s*\{[^}]*gap:\s*0[^}]*border:\s*1px solid var\(--color-settings-border\)[^}]*border-radius:\s*var\(--radius-xl\)[^}]*background:\s*var\(--color-surface-card\)/s,
  );
  assert.match(
    settingsSectionCss,
    /\.settings-section__items--grouped > \.settings-page__item \+ \.settings-page__item,[\s\S]*border-top:\s*1px solid var\(--color-settings-border\)/,
  );
  assert.doesNotMatch(generalDefinition, /groupedRows|separatedRows/);
  assert.match(modelsDefinition, /id: 'model-manager',[\s\S]{0,80}separatedRows: true/);
  assert.ok(
    modelsDefinition.indexOf("id: 'model-manager'") < modelsDefinition.indexOf("id: 'free-models'"),
    'free models should render after the chat model manager',
  );
  assert.match(channelsDefinition, /id: 'channels',[\s\S]{0,80}separatedRows: true/);
  assert.match(modelsSettings, /<SettingsSection[\s\S]{0,120}separatedRows/);
  assert.match(channelList, /<SettingsSection separatedRows>/);
  assert.match(
    buttonCss,
    /\.ui-button--icon-only\s*\{[^}]*width:\s*32px[^}]*height:\s*32px[^}]*border:\s*0[^}]*background:\s*transparent/s,
  );
  assert.match(
    buttonCss,
    /\.ui-button\s*\{[^}]*min-height:\s*32px[^}]*padding:\s*4px 24px[^}]*font-size:\s*14px[^}]*font-weight:\s*400[^}]*line-height:\s*22px[^}]*border:\s*1px solid var\(--color-button-border\)[^}]*border-radius:\s*var\(--radius-full\)/s,
  );
  assert.match(
    buttonCss,
    /\.ui-button--sm\s*\{[^}]*min-height:\s*28px[^}]*padding:\s*4px 16px[^}]*font-size:\s*12px[^}]*line-height:\s*18px/s,
  );
  assert.match(
    settingsPageCss,
    /\.settings-page \.settings-inline-input \.ui-button\s*\{[^}]*flex:\s*0 0 auto[^}]*white-space:\s*nowrap/s,
  );
  assert.match(buttonCss, /\.ui-button--icon-only\s*\{[^}]*border-radius:\s*8px/s);
  assert.match(
    buttonCss,
    /\.ui-button--primary\s*\{[^}]*color:\s*var\(--color-control-emphasis-text\)[^}]*border-color:\s*var\(--color-control-emphasis\)[^}]*background:\s*var\(--color-control-emphasis\)/s,
  );
  assert.doesNotMatch(
    settingsPageCss,
    /\.settings-page \.ui-button(?:--(?:primary|quiet|warning|danger|sm|icon-only))?\s*\{/,
  );
  assert.equal((formDialog.match(/<Button\s+[\s\S]*?size="sm"/g) ?? []).length >= 2, true);
  assert.match(formDialogCss, /\.form-dialog__actions \.ui-button\s*\{[^}]*min-width:\s*84px/s);
  assert.equal((settingsConfirmDialog.match(/<Button[^>]*size="sm"/g) ?? []).length, 2);
  assert.match(
    settingsConfirmDialogCss,
    /\.settings-confirm-dialog__footer \.ui-button\s*\{[^}]*min-width:\s*84px/s,
  );
  assert.match(lightTheme, /--color-settings-switch-checked:\s*#1476ff;/i);
  assert.match(
    settingsPageCss,
    /\.settings-page \.ui-switch--checked\s*\{[^}]*border-color:\s*var\(--color-settings-switch-checked\)[^}]*background:\s*var\(--color-settings-switch-checked\)/s,
  );
  assert.match(
    settingsPageCss,
    /\.settings-models__toast--success\s*\{[^}]*border-color:\s*var\(--color-feedback-success\)[^}]*background:\s*var\(--color-feedback-success-toast\)/s,
  );
  assert.match(
    settingsPageCss,
    /\.settings-models__toast--error\s*\{[^}]*border-color:\s*var\(--color-feedback-danger\)[^}]*background:\s*var\(--color-feedback-danger-toast\)/s,
  );
  assert.match(
    settingsPageCss,
    /\.settings-model-group\s*\{[^}]*border:\s*1px solid var\(--color-settings-border\)[^}]*border-radius:\s*var\(--radius-xl\)[^}]*background:\s*var\(--color-surface-card\)/s,
  );
  assert.match(
    settingsPageCss,
    /\.settings-model-group__items > \.settings-model-card--grouped\s*\{[^}]*border:\s*0[^}]*border-radius:\s*0[^}]*background:\s*transparent/s,
  );
  assert.match(settingsPageCss, /container:\s*settings-models-list\s*\/\s*inline-size/);
  assert.match(settingsPageCss, /\.settings-model-card__actions\s*\{[^}]*flex-wrap:\s*nowrap/s);
  assert.match(settingsPageCss, /@container\s+settings-models-list\s*\(max-width:\s*36rem\)/);
  assert.doesNotMatch(settingsPageCss, /@media\s*\(max-width:\s*1280px\)\s*\{[^}]*\.settings-model-card/s);
  assert.match(lightTheme, /--color-feedback-success-toast:\s*#[\da-f]{6};/i);
  assert.match(lightTheme, /--color-feedback-danger-toast:\s*#[\da-f]{6};/i);
  assert.match(settingRowCss, /min-height:\s*80px/);
  assert.match(settingRowCss, /padding:\s*16px/);
  assert.match(settingRowCss, /\.setting-row__title\s*\{[^}]*font-size:\s*14px[^}]*line-height:\s*22px/s);
  assert.match(
    settingsPageCss,
    /\.settings-model-card\s*\{[^}]*grid-template-columns:\s*40px minmax\(0, 1fr\) auto[^}]*min-height:\s*80px[^}]*padding:\s*16px[^}]*border-radius:\s*var\(--radius-xl\)/s,
  );
  assert.match(settingRowCss, /container:\s*setting-row\s*\/\s*inline-size/);
  assert.match(settingRowCss, /@container\s+setting-row\s*\(max-width:\s*30rem\)/);
  assert.doesNotMatch(settingRowCss, /@media\s*\(max-width:\s*960px\)/);
  assert.match(formCss, /\.form-item__control\s*>\s*\.ui-select\s*\{[^}]*width:\s*100%/s);
  assert.match(modelProviderSelect, /getProviderLogoUrl/);
  assert.match(modelsSettings, /logo: getModelLogoUrl\(model\)/);
  assert.doesNotMatch(modelsSettings, /getConfiguredProviderLogoUrl/);
  assert.match(providerAssets, /VENDOR_ICON_KEYS/);
  assert.match(providerAssets, /\['openrouter', 'openrouter'\]/);
  assert.match(providerAssets, /model\.is_free === true/);
  assert.match(providerAssets, /model\.model_provider === 'OpenAIAccount'/);
  assert.match(providerAssets, /model\.vendor_key\?\.trim\(\)/);
  assert.match(modelProviderIcon, /return getModelLogoUrl\(model\)/);
  assert.doesNotMatch(modelProviderIcon, /PROVIDER_SPECS|findProvider|keywordMatchesModelName/);
  assert.match(modelProviderSelect, /getVendorLogoUrl\(selected\.preset\.vendor_key\)/);
  assert.match(modelProviderSelect, /getVendorLogoUrl\(option\.preset\.vendor_key\)/);
  assert.doesNotMatch(modelProviderSelect, /preset\.icon_key/);
  assert.match(settingsAssets, /settingsCustomModelIcon/);
  assert.equal(existsSync(new URL('src/assets/settings/providers/', root)), false);
  const providerAssetStems = new Set();
  for (const providerAsset of readdirSync(new URL('src/assets/providers/', root))) {
    if (!/\.(?:png|svg)$/.test(providerAsset)) continue;
    const stem = providerAsset.replace(/\.(?:png|svg)$/, '');
    assert.equal(providerAssetStems.has(stem), false, `${stem} must have exactly one standard provider asset`);
    providerAssetStems.add(stem);
  }
  for (const iconKey of [
    'anthropic',
    'deepseek',
    'kimi',
    'mimo',
    'minimax',
    'openai',
    'openrouter',
    'pangu',
    'qwen',
    'tencent-cloud',
    'zhipu',
  ]) {
    assert.equal(existsSync(new URL(`src/assets/providers/${iconKey}.svg`, root)), true);
    assert.equal(existsSync(new URL(`src/assets/providers/${iconKey}.png`, root)), false);
  }
  assert.match(modelProviderSelect, /settings-model-provider-select__menu/);
  assert.match(modelProviderSelect, /role="listbox"/);
  assert.match(modelDialog, /confirmDisabled=\{/);
  assert.doesNotMatch(modelDialog, /confirmDisabled=\{[^}]*hasUnsavedChanges/);
  assert.match(modelDialog, /useSettingsFormDialogClose\(\{[\s\S]{0,260}const values = form\.getValues\(\)/);
  assert.match(channelsCss, /width:\s*min\(560px,\s*calc\(100vw - 32px\)\)/);
  assert.match(
    channelsCss,
    /\.settings-channels-panel__channel-list,[\s\S]*gap:\s*0[^}]*border:\s*1px solid var\(--color-settings-border\)[^}]*border-radius:\s*var\(--radius-xl\)/,
  );
  assert.match(
    channelsCss,
    /\.settings-channels-panel__channel-card\s*\{[^}]*min-height:\s*80px[^}]*gap:\s*20px[^}]*padding:\s*16px[^}]*border:\s*0[^}]*border-radius:\s*0[^}]*background:\s*transparent/s,
  );
  assert.match(
    channelsCss,
    /\.settings-channels-panel__channel-details\s*\{[^}]*display:\s*flex[^}]*margin-top:\s*2px[^}]*gap:\s*12px/s,
  );
  assert.match(
    channelsCss,
    /\.settings-channels-panel__configuration-guide\s*\{[^}]*color:\s*var\(--color-settings-link\)[^}]*font-size:\s*14px[^}]*line-height:\s*22px/s,
  );
  assert.match(
    channelsCss,
    /\.settings-page \.ui-button\.settings-channels-panel__configure-button\s*\{[^}]*width:\s*auto[^}]*min-width:\s*84px[^}]*height:\s*28px[^}]*min-height:\s*28px[^}]*border:\s*1px solid var\(--color-button-border\)[^}]*border-radius:\s*var\(--radius-full\)/s,
  );
  assert.match(channelsCss, /\.settings-channels-panel__accounts\s*\{[^}]*margin-top:\s*22px[^}]*gap:\s*16px/s);
  assert.match(
    channelsCss,
    /\.settings-channels-panel__account-card\s*\{[^}]*min-height:\s*72px[^}]*padding:\s*12px 16px[^}]*background:\s*var\(--color-settings-card-subtle\)/s,
  );
  assert.match(channelsCss, /\.settings-channels-panel__account-logo,[^}]*width:\s*42px[^}]*height:\s*42px/s);
  assert.doesNotMatch(channelsCss, /\.settings-channels-panel__account-copy span/);
  assert.match(channelsCss, /\.settings-channels-panel__account-actions\s*\{[^}]*display:\s*flex[^}]*gap:\s*8px/s);
  assert.match(
    channelsCss,
    /\.settings-page \.ui-button\.settings-channels-panel__account-action\s*\{[^}]*width:\s*32px[^}]*height:\s*32px[^}]*padding:\s*8px[^}]*border:\s*0[^}]*background:\s*transparent/s,
  );
  assert.match(lightTheme, /--color-button-border:\s*#595959;/i);
  assert.match(lightTheme, /--color-button-hover:\s*#fafafa;/i);
  assert.doesNotMatch(channelsModule, /descriptionKey/);
  assert.match(channelList, /<article[^>]*className=\{`settings-channels-panel__channel-card/s);
  assert.doesNotMatch(channelList, /<button[^>]*className=\{`settings-channels-panel__channel-card/s);
  assert.match(channelList, /data-state=\{configured \? 'configured' : 'unconfigured'\}/);
  assert.match(channelList, /className="settings-channels-panel__channel-details"/);
  assert.match(channelList, /className="settings-channels-panel__add-configuration"/);
  assert.match(channelList, /className="settings-channels-panel__account-actions"/);
  assert.match(channelList, /import \{ Button, Tag \} from '[^']*components\/ui'/);
  assert.match(channelList, /<Tag variant=\{account\.configured \? 'success' : 'neutral'\}>/);
  assert.match(channelList, /<Tag variant=\{account\.enabled \? 'success' : 'neutral'\}>/);
  assert.match(channelList, /t\('common\.modify'\)/);
  assert.match(channelList, /t\('channels\.unbind'\)/);
  assert.match(channelList, /title=\{t\(account\.enabled \? 'channels\.disable' : 'channels\.enable'\)\}/);
  assert.match(channelList, /const EnableIcon = settingsActionIcons\.enable/);
  assert.match(channelList, /const DisableIcon = settingsActionIcons\.disable/);
  assert.match(channelList, /icon=\{account\.enabled \? <DisableIcon aria-hidden \/> : <EnableIcon aria-hidden \/>\}/);
  assert.match(
    channelList,
    /className="settings-channels-panel__configure-button"[^>]*onClick=\{\(\) => onConfigure\(channel\.channel_id\)\}/s,
  );
  assert.match(
    channelList,
    /className="settings-channels-panel__account-action"[\s\S]*?onClick=\{\(\) => onEdit\(channel\.channel_id, account\.index\)\}/,
  );
  assert.match(
    channelList,
    /className="settings-channels-panel__account-action settings-channels-panel__account-action--danger"[\s\S]*?onClick=\{\(\) => onUnbind\(channel\.channel_id, account\.index, account\.name\)\}/,
  );
  assert.match(channelList, /import \{ Unlink \} from 'lucide-react'/);
  assert.match(channelList, /icon=\{<Unlink aria-hidden \/>\}/);
  assert.doesNotMatch(
    channelList,
    /settingsActionIcons\.delete|DeleteIcon|>\s*\{t\('channels\.unbind'\)\}\s*<\/Button>/,
  );
  assert.match(channelList, /t\('channels\.boundSuccess'\)/);
  assert.match(channelList, /t\('channels\.status\.disabled'\)/);
  assert.match(channelList, /className="settings-channels-panel__add-configuration"[^>]*onClick=\{onAddFeishu\}/s);
  const channelController = source('src/features/settings/modules/channels/useSettingsChannelsController.ts');
  assert.match(
    channelController,
    /const requestDeletion = \(channelId: SettingsChannelId, accountIndex: number, accountName: string\)/,
  );
  assert.match(channelController, /buildFeishuDeletionPayload\(feishu\.form\.getValues\(\), accountIndex\)/);
  assert.match(channelController, /buildSingleChannelDeletionPayload\(channelId\)/);
  assert.match(channelController, /replaceAndSave\(/);
  assert.doesNotMatch(channelController, /enabled:\s*false/);
  assert.match(
    channelDialog,
    /<FeishuChannelForm ref=\{feishuFormRef\} controller=\{controllers\.feishu\} appIndex=\{activeFeishuAppIndex\} \/>/,
  );
  assert.match(feishuForm, /data-testid="settings-channels-panel-feishu-app-form"/);
  assert.match(feishuForm, /<Form/);
  assert.match(feishuForm, /createChannelFormRules\('feishu'/);
  assert.match(channelDialog, /feishuFormRef\.current\?\.validate\(\) !== true/);
  assert.doesNotMatch(feishuForm, /Chevron|Plus|Trash|addApp|deleteApp|expandedIndex/);
  assert.doesNotMatch(feishuForm, /settings-channels-panel__feishu-apps/);
  assert.doesNotMatch(channelFormItems, /name:\s*['"]enabled['"]/);
  assert.match(channelFormItems, /isChannelFormFieldOptional/);
  assert.doesNotMatch(channelFormItems, /required:\s*(?:true|false)/);
  assert.match(channelRequirements, /频道字段业务规则的唯一事实源/);
  assert.match(channelRequirements, /ak:\s*'required'/);
  assert.match(channelRequirements, /app_id:\s*'required'/);
  assert.match(standardChannelForm, /rules=\{rules\}/);
  assert.doesNotMatch(standardChannelForm, /showOptional=\{false\}/);
  assert.equal(zh.channels.unbindConfigurationTitle, '解绑频道配置');
  assert.equal(en.channels.unbindConfigurationTitle, 'Unbind channel configuration');
  assert.equal(en.channels.boundSuccess, 'Bound successfully');
  assert.equal(en.channels.status.disabled, 'Not enabled');
  assert.match(channelDialog, /<FormDialog/);
  assert.match(standardChannelForm, /<Form/);
  assert.match(channelList, /<SettingsSection/);
  assert.doesNotMatch(channelDialog, /secondaryAction|channel-config-refresh-btn/);
  assert.doesNotMatch(channelDialog, /settings-channel-dialog__success|channel-config-success/);
  assert.match(channelDialog, /if \(!open\) return null;/);
  assert.match(channelDialog, /if \(await controller\.save\(\)\) onSaved\(\);/);
  assert.match(channelController, /if \(!\(await targetController\.load\(\)\)\) return;/);
  assert.match(
    channelController,
    /const closeDialogAfterSave = \(\) => \{\s*resetActiveDialog\(\);\s*setDialogOpen\(false\);/s,
  );

  assert.match(sidebar, /assets\/settings\/app-navigation\/settings\.svg\?react/);
  assert.match(appSettingsIcon, /<circle[^>]*cx="6"[^>]*cy="5"[^>]*r="2"/);
  assert.match(appSettingsIcon, /<circle[^>]*cx="10"[^>]*cy="11"[^>]*r="2"/);
  assert.match(appSettingsIcon, /currentColor/);
  assert.doesNotMatch(appSettingsIcon, /rgb\(25,25,25\)/);

  for (const asset of [
    'src/assets/settings/navigation/general.svg',
    'src/assets/settings/navigation/models.svg',
    'src/assets/settings/navigation/agent.svg',
    'src/assets/settings/navigation/browser.svg',
    'src/assets/settings/navigation/channels.svg',
    'src/assets/settings/navigation/experimental.svg',
    'src/assets/settings/channels/xiaoyi.svg',
    'src/assets/settings/channels/feishu.svg',
    'src/assets/settings/channels/dingtalk.svg',
    'src/assets/settings/channels/telegram.svg',
    'src/assets/settings/channels/discord.svg',
    'src/assets/settings/channels/slack.svg',
    'src/assets/settings/channels/whatsapp.svg',
    'src/assets/settings/actions/refresh.svg',
    'src/assets/settings/actions/edit.svg',
    'src/assets/settings/actions/enable.svg',
    'src/assets/settings/actions/disable.svg',
    'src/assets/settings/empty/empty-box.svg',
  ]) {
    assert.equal(existsSync(new URL(asset, root)), true, `${asset} must exist`);
  }

  assert.match(channelsPanel, /SettingsConfirmDialog/);
  assert.doesNotMatch(channelsPanel, /window\.confirm/);
  assert.match(
    channelList,
    /<a[\s\S]*href=\{getSettingsChannelGuideUrl\(channel\.channel_id, guideLanguage\)\}[\s\S]*target="_blank"[\s\S]*rel="noopener noreferrer"/,
  );
  const catalog = source('src/features/settings/modules/channels/channelCatalog.ts');
  for (const channelId of ['xiaoyi', 'feishu', 'dingtalk', 'telegram', 'discord', 'slack', 'whatsapp'])
    assert.match(catalog, new RegExp(`'${channelId}'`));
});

test('Settings channel implementation stays decomposed around shared form capabilities', () => {
  const files = {
    panel: 'src/features/settings/modules/channels/SettingsChannelsPanel.tsx',
    controller: 'src/features/settings/modules/channels/useSettingsChannelsController.ts',
    adapters: 'src/features/settings/modules/channels/channelAdapters.ts',
    formItems: 'src/features/settings/modules/channels/channelFormItems.ts',
    feishu: 'src/features/settings/modules/channels/forms/FeishuChannelForm.tsx',
  };
  assert.ok(source(files.panel).split('\n').length <= 100, 'SettingsChannelsPanel must remain a composition root');
  for (const [name, path] of Object.entries(files)) {
    assert.ok(source(path).split('\n').length <= 450, `${name} must stay below 450 lines`);
  }
  assert.match(source(files.panel), /useSettingsChannelsController/);
  assert.match(source('src/features/settings/modules/channels/useChannelForm.ts'), /useForm/);
  assert.doesNotMatch(source(files.panel), /channel\.[a-z]+\.(?:get|set)_conf/);
});

test('Xiaoyi enable confirmation keeps continue, edit, and dismiss as distinct actions', () => {
  const controller = source('src/features/settings/modules/channels/useSettingsChannelsController.ts');
  const xiaoyiConfirmation = source(
    'src/features/settings/modules/channels/components/XiaoyiEnableConfirmDialog.tsx',
  );
  const confirmDialog = source('src/features/settings/components/SettingsConfirmDialog.tsx');

  assert.match(controller, /shouldConfirmXiaoyiEnable\(enabled, xiaoyi\.form\.getValues\(\)\.api_id\)/);
  assert.match(controller, /confirmXiaoyiEnable[\s\S]*enabled: true[\s\S]*setPendingXiaoyiEnable\(null\)/);
  assert.match(xiaoyiConfirmation, /onConfirm=\{\(\) => void controller\.confirmXiaoyiEnable\(\)\}/);
  assert.match(xiaoyiConfirmation, /onCancel=\{controller\.editPendingXiaoyiConfiguration\}/);
  assert.match(xiaoyiConfirmation, /onDismiss=\{controller\.cancelXiaoyiEnable\}/);
  assert.match(confirmDialog, /<Dialog[\s\S]*onCancel=\{onDismiss\}/);
});

test('legacy channel and More surfaces are removed while Settings owns their replacements', () => {
  const sidebar = source('src/components/SessionSidebar/index.tsx');
  const app = source('src/App.tsx');
  const settingsChannelsModule = source('src/features/settings/modules/channels/ChannelsModule.tsx');
  const settingsNavigation = source('src/features/settings/settingsNavigation.ts');
  const modelDialog = source('src/features/settings/modules/models/ModelDialog.tsx');
  const mainNavItems = sidebar.slice(sidebar.indexOf('const mainNavItems'), sidebar.indexOf('export function SessionSidebar'));
  assert.match(mainNavItems, /key: 'updatepanel'/);
  assert.match(mainNavItems, /key: 'settings'/);
  assert.doesNotMatch(mainNavItems, /key: 'channels'/);
  assert.doesNotMatch(sidebar, /moreNavItems|configpanel|browserpanel|key: 'extensions'/);
  assert.match(app, /<SettingsPage/);
  assert.match(app, /initialModuleId=\{requestedSettingsModuleId \?\? undefined\}/);
  assert.doesNotMatch(app, /ConfigPanel|BrowserPanel|ChannelsPanel|ExtensionsHubPanel/);
  assert.match(settingsChannelsModule, /import \{ SettingsChannelsPanel \} from '\.\/SettingsChannelsPanel'/);
  assert.doesNotMatch(settingsChannelsModule, /components\/ChannelsPanel/);
  assert.match(settingsNavigation, /export type SettingsModuleTarget = 'models' \| 'agent'/);
  assert.match(settingsNavigation, /SETTINGS_MODULE_NAVIGATION_EVENT = 'jiuwen:settings-module'/);
  assert.equal(existsSync(new URL('src/components/ConfigPanel/index.tsx', root)), false);
  assert.equal(existsSync(new URL('src/components/BrowserPanel/index.tsx', root)), false);
  assert.equal(existsSync(new URL('src/components/ChannelsPanel/index.tsx', root)), false);
  assert.equal(existsSync(new URL('src/components/ExtensionsHubPanel/index.tsx', root)), false);
  assert.match(modelDialog, /OpenAIAccountSettings, useOpenAIAccountController/);
  assert.equal(existsSync(new URL('src/features/settings/modules/models/OpenAIAccountField.tsx', root)), true);
});

test('legacy page translations and Harness package state are removed without deleting shared Settings keys', () => {
  const supportedChannelIds = ['dingtalk', 'discord', 'feishu', 'slack', 'telegram', 'whatsapp', 'xiaoyi'];
  const expectedConfigSections = [
    'booleanLabels',
    'enterValue',
    'externalCli',
    'keyHelp',
    'modelList',
    'openaiAccount',
    'proactive',
  ];

  for (const locale of [zh, en]) {
    assert.deepEqual(Object.keys(locale.browser).sort(), [
      'browserTypeAuto',
      'browserTypeChrome',
      'browserTypeEdge',
      'errors',
    ]);
    assert.deepEqual(Object.keys(locale.channels.labels).sort(), supportedChannelIds);
    assert.deepEqual(Object.keys(locale.config).sort(), expectedConfigSections);
    assert.equal(locale.extensions, undefined);
    assert.equal(locale.harnessPackage, undefined);
    assert.equal(locale.sessionSidebar.moreSettings, undefined);
    for (const key of ['noExtension', 'refreshFiles', 'loadFailed']) {
      assert.equal(locale.toolPanel[key], undefined, `toolPanel.${key} must be absent`);
    }
    for (const key of ['channels', 'extensions', 'rails', 'harnesspkg', 'config', 'browser']) {
      assert.equal(locale.nav[key], undefined, `nav.${key} must be absent`);
    }
    for (const key of ['title', 'subtitle', 'listTitle', 'listMeta', 'loading', 'wechatLogin', 'wechatUnbind']) {
      assert.equal(locale.channels[key], undefined, `channels.${key} must be absent`);
    }
    assert.equal(locale.channels.config.wechatTitle, undefined);
    assert.equal(locale.channels.config.wecomTitle, undefined);
    assert.equal(locale.nav.more.length > 0, true);
    assert.equal(locale.channels.config.xiaoyiTitle.length > 0, true);
    assert.equal(locale.config.openaiAccount.title.length > 0, true);
  }

  const types = source('src/types/index.ts');
  const harnessStore = source('src/stores/harnessStore.ts');
  const storeExports = source('src/stores/index.ts');
  const utilityExports = source('src/utils/index.ts');
  assert.equal(existsSync(new URL('src/components/ToolPanel/HarnessExtensionTree.tsx', root)), false);
  assert.equal(existsSync(new URL('src/utils/harnessErrors.ts', root)), false);
  assert.doesNotMatch(types, /interface (?:PackageInfo|NativeVersionInfo|PackagesPayload|ActivatePayload|DeactivatePayload)\b/);
  assert.doesNotMatch(harnessStore, /CachedFileTreeEntry/);
  assert.doesNotMatch(storeExports, /CachedFileTreeEntry/);
  assert.doesNotMatch(utilityExports, /harnessErrors/);
  assert.doesNotMatch(
    harnessStore,
    /\b(?:CachedFileTreeEntry|packages|nativeVersion|activePackageIds|selectedPackageId|loadingPackages|activatingPackage|deactivatingPackage|extensionFileTreeCache|fileTreeLoadingPaths|setPackages|isPackageActive|setSelectedPackageId|setLoadingPackages|setActivatingPackage|setDeactivatingPackage|setFileTreeCache|getFileTreeCache|clearFileTreeCache|setFileTreeLoading|isFileTreeLoading)\s*:/,
  );
});
