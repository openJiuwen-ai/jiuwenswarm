import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

await build({
  stdin: {
    contents: "export * from './src/features/settings/modules/appearance/appearancePreferences';",
    resolveDir: process.cwd(),
    loader: 'ts',
  },
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outfile: 'node_modules/.cache/appearance-preferences/appearancePreferences.mjs',
});

const {
  APPEARANCE_STORAGE_KEY,
  UI_FONT_PRESETS,
  CODE_FONT_PRESETS,
  defaultAppearancePreferences,
  normalizeFontList,
  clampFontScale,
  resolveFontStack,
  computeFontSizeVars,
  readAppearancePreferences,
  saveAppearancePreferences,
  clearAppearancePreferences,
  applyAppearancePreferences,
} = await import('../node_modules/.cache/appearance-preferences/appearancePreferences.mjs');

function memoryStorage(initial = new Map()) {
  return {
    getItem: (key) => (initial.has(key) ? initial.get(key) : null),
    setItem: (key, value) => void initial.set(key, String(value)),
    removeItem: (key) => void initial.delete(key),
  };
}

function styleTarget() {
  const props = new Map();
  return {
    setProperty: (name, value) => props.set(name, value),
    removeProperty: (name) => {
      const previous = props.get(name) ?? '';
      props.delete(name);
      return previous;
    },
    props,
  };
}

test('normalizeFontList: 正常多字体串会规范化为逗号分隔列表', () => {
  assert.equal(normalizeFontList('  Foo  , Bar , Baz '), 'Foo, Bar, Baz');
});

test('normalizeFontList: 剥离引号与反引号', () => {
  assert.equal(normalizeFontList('"JetBrains Mono", \'Fira Code\', `Cascadia`'), 'JetBrains Mono, Fira Code, Cascadia');
});

test('normalizeFontList: 剔除注入字符（分号、花括号、尖括号、反斜杠）与控制字符', () => {
  assert.equal(normalizeFontList('Bad;Font {} <> \\ still ok'), 'BadFont still ok');
  assert.equal(normalizeFontList('A\u0000B\u001fC\u007fD'), 'ABCD');
});

test('normalizeFontList: 最多保留 8 个族，每个族截断到 64 字符，通用字体族保留关键字形式', () => {
  const nine = Array.from({ length: 9 }, (_, i) => `Font${i}`).join(',');
  assert.equal(normalizeFontList(nine).split(',').length, 8);
  assert.equal(normalizeFontList(nine), 'Font0, Font1, Font2, Font3, Font4, Font5, Font6, Font7');
  const long = 'x'.repeat(80);
  assert.equal(normalizeFontList(long).length, 64);
  assert.equal(normalizeFontList('SANS-SERIF, Fancy Serif'), 'sans-serif, Fancy Serif');
  assert.equal(normalizeFontList(42), '');
});

test('clampFontScale: 上下界钳制到 90–140', () => {
  assert.equal(clampFontScale(150), 140);
  assert.equal(clampFontScale(80), 90);
  assert.equal(clampFontScale(90), 90);
  assert.equal(clampFontScale(140), 140);
});

test('clampFontScale: 字符串 / NaN / undefined / 空串回退默认 100', () => {
  assert.equal(clampFontScale('abc'), 100);
  assert.equal(clampFontScale(Number.NaN), 100);
  assert.equal(clampFontScale(undefined), 100);
  assert.equal(clampFontScale(null), 100);
  assert.equal(clampFontScale(''), 100);
  assert.equal(clampFontScale('125'), 125);
});

test('clampFontScale: 两位小数取整', () => {
  assert.equal(clampFontScale(105.678), 105.68);
  assert.equal(clampFontScale('125.999'), 126);
  assert.equal(clampFontScale(90.004), 90);
});

test('resolveFontStack: default 与未知预设返回 null（沿用主题默认）', () => {
  assert.equal(resolveFontStack('default', '', UI_FONT_PRESETS), null);
  assert.equal(resolveFontStack('not-a-preset', 'Foo', UI_FONT_PRESETS), null);
  assert.equal(resolveFontStack(undefined, 'Foo', CODE_FONT_PRESETS), null);
});

test('resolveFontStack: 预设栈命中返回期望字体栈', () => {
  assert.equal(resolveFontStack('zh-song', '', UI_FONT_PRESETS), UI_FONT_PRESETS['zh-song']);
  assert.ok(resolveFontStack('zh-kai', '', UI_FONT_PRESETS).includes('LXGW WenKai'));
  assert.equal(resolveFontStack('jetbrains-fira', '', CODE_FONT_PRESETS), CODE_FONT_PRESETS['jetbrains-fira']);
});

test('resolveFontStack: 自定义为空回退默认，非空返回归一化结果', () => {
  assert.equal(resolveFontStack('custom', '', UI_FONT_PRESETS), null);
  assert.equal(resolveFontStack('custom', '  ', CODE_FONT_PRESETS), null);
  assert.equal(resolveFontStack('custom', '"LXGW WenKai", serif', UI_FONT_PRESETS), 'LXGW WenKai, serif');
});

test('computeFontSizeVars: 100% 与 foundation.css 现状完全一致（14/16/13/12/11px）', () => {
  assert.deepEqual(computeFontSizeVars(100), {
    base: '14px',
    lg: '16px',
    sm: '13px',
    xs: '12px',
    '2xs': '11px',
  });
});

test('computeFontSizeVars: 125% 时 base=17.5px，其余等比换算并保留两位小数', () => {
  const sizes = computeFontSizeVars(125);
  assert.equal(sizes.base, '17.5px');
  assert.equal(sizes.lg, '20px');
  assert.equal(sizes.sm, '16.25px');
  assert.equal(sizes.xs, '15px');
  assert.equal(sizes['2xs'], '13.75px');
});

test('readAppearancePreferences: 损坏 JSON、非对象与 storage 抛异常时回退默认值', () => {
  assert.deepEqual(readAppearancePreferences(memoryStorage(new Map([['k', '{broken json']]))), {
    ...defaultAppearancePreferences,
  });
  const wrongShape = memoryStorage(new Map([['k', '["array"]']]));
  assert.deepEqual(readAppearancePreferences(wrongShape), { ...defaultAppearancePreferences });
  const throwingStorage = {
    getItem: () => {
      throw new Error('boom');
    },
    setItem: () => {},
    removeItem: () => {},
  };
  assert.deepEqual(readAppearancePreferences(throwingStorage), { ...defaultAppearancePreferences });
  assert.deepEqual(readAppearancePreferences(null), { ...defaultAppearancePreferences });
  assert.equal(readAppearancePreferences(null).uiFontPreset, 'default');
});

test('保存-读取往返成功，清空后恢复默认', () => {
  const storage = memoryStorage();
  const prefs = { uiFontPreset: 'zh-kai', uiFontCustom: '', fontScale: 125, codeFontPreset: 'custom', codeFontCustom: 'Fira Code' };
  saveAppearancePreferences(storage, prefs);
  assert.equal(storage.getItem(APPEARANCE_STORAGE_KEY), JSON.stringify(prefs));
  const loaded = readAppearancePreferences(storage);
  assert.equal(loaded.uiFontPreset, 'zh-kai');
  assert.equal(loaded.fontScale, 125);
  assert.equal(loaded.codeFontCustom, 'Fira Code');

  const throwingWrite = {
    getItem: () => null,
    setItem: () => {
      throw new Error('boom');
    },
    removeItem: () => {},
  };
  saveAppearancePreferences(throwingWrite, prefs); // 不应抛错
  clearAppearancePreferences(throwingWrite); // 不应抛错

  clearAppearancePreferences(storage);
  assert.equal(storage.getItem(APPEARANCE_STORAGE_KEY), null);
  assert.deepEqual(readAppearancePreferences(storage), { ...defaultAppearancePreferences });
});

test('applyAppearancePreferences: 缺少 target 不抛错；正常 target 按偏好设置/移除 CSS 变量', () => {
  assert.doesNotThrow(() => applyAppearancePreferences({ ...defaultAppearancePreferences, fontScale: 125 }, null));

  const target = styleTarget();
  applyAppearancePreferences({ ...defaultAppearancePreferences, fontScale: 100 }, target);
  assert.equal(target.props.size, 0);

  target.setProperty('--font-body', 'stale');
  target.setProperty('--font-size-base', '20px');
  applyAppearancePreferences({ ...defaultAppearancePreferences, fontScale: 100 }, target);
  assert.equal(target.props.has('--font-body'), false);
  assert.equal(target.props.has('--font-size-base'), false);

  const custom = {
    uiFontPreset: 'custom',
    uiFontCustom: '"LXGW WenKai", serif',
    fontScale: 140,
    codeFontPreset: 'cascadia-code',
    codeFontCustom: '',
  };
  applyAppearancePreferences(custom, target);
  assert.equal(target.props.get('--font-body'), 'LXGW WenKai, serif');
  assert.equal(target.props.get('--font-display'), 'LXGW WenKai, serif');
  assert.equal(target.props.get('--font-mono'), CODE_FONT_PRESETS['cascadia-code']);
  assert.equal(target.props.get('--font-size-base'), '19.6px');
  assert.equal(target.props.get('--font-size-2xs'), '15.4px');
});
