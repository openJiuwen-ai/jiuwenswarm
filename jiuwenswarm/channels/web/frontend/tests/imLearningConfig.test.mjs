/**
 * imLearningConfig 纯函数单测（node --test，项目惯例）。
 *
 * 覆盖：preset ↔ since_ms 换算与漂移容忍、title 不参与语义脏检查（对齐后端
 * _semantic_config_dump）、合并去重、discover diff 标记、批量粘贴解析、
 * 草稿校验（对齐后端 ImLearningConfig 校验规则）。
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import {
  IM_LEARNING_INTERVAL_MAX_SECONDS,
  discoverItemToTarget,
  imLearningSignature,
  isImLearningDirty,
  markDiscoveredItems,
  mergeTargets,
  parseBulkExternalIds,
  presetDaysToSinceMs,
  sinceMsToPreset,
  targetKey,
  validateImLearningDraft,
} from '../node_modules/.cache/im-learning-config/imLearningConfig.mjs';

const NOW = 1_700_000_000_000;
const DAY_MS = 86_400_000;

function config(overrides = {}) {
  return {
    enabled: false,
    targets: [
      { channel_id: 'welink', kind: 'group', external_id: 'G1', title: '项目群' },
    ],
    since_ms: null,
    fetch_interval_seconds: 600,
    fetch_top_n: 50,
    ...overrides,
  };
}

test('targetKey 是三元组键，title 不参与', () => {
  assert.equal(
    targetKey({ channel_id: 'welink', kind: 'group', external_id: 'G1' }),
    'welink:group:G1',
  );
  assert.equal(
    targetKey({ channel_id: 'welink', kind: 'user', external_id: 'G1' }),
    'welink:user:G1',
  );
});

test('preset 与 since_ms 互换：null 不限、四个 preset、非 preset 回显自定义', () => {
  assert.equal(sinceMsToPreset(null, NOW), null);
  for (const days of [30, 90, 180, 365]) {
    assert.equal(sinceMsToPreset(NOW - days * DAY_MS, NOW), days);
  }
  // 漂移容忍（半天内）：保存后即时回读仍识别为 preset。
  assert.equal(sinceMsToPreset(NOW - 30 * DAY_MS + 60_000, NOW), 30);
  assert.equal(sinceMsToPreset(NOW - 30 * DAY_MS - 60_000, NOW), 30);
  // 超出容忍或任意值 → 自定义。
  assert.equal(sinceMsToPreset(NOW - 30 * DAY_MS + DAY_MS, NOW), 'custom');
  assert.equal(sinceMsToPreset(123, NOW), 'custom');
  // preset → since_ms 以 now 为锚点向前取整天数。
  assert.equal(presetDaysToSinceMs(90, NOW), NOW - 90 * DAY_MS);
});

test('仅改 title 不算脏（对齐后端等价快路径），其余字段敏感', () => {
  const saved = config();
  const titleOnly = config({
    targets: [
      { channel_id: 'welink', kind: 'group', external_id: 'G1', title: '新标题' },
    ],
  });
  assert.equal(isImLearningDirty(saved, titleOnly), false);
  assert.equal(imLearningSignature(saved), imLearningSignature(titleOnly));

  // targets 顺序无关（集合语义）。
  const reordered = config({
    targets: [
      { channel_id: 'feishu', kind: 'user', external_id: 'U1' },
      { channel_id: 'welink', kind: 'group', external_id: 'G1', title: '项目群' },
    ],
  });
  const reordered2 = config({
    targets: [
      { channel_id: 'welink', kind: 'group', external_id: 'G1', title: '项目群' },
      { channel_id: 'feishu', kind: 'user', external_id: 'U1' },
    ],
  });
  assert.equal(isImLearningDirty(reordered, reordered2), false);

  // 各语义字段变化都算脏。
  assert.equal(isImLearningDirty(saved, config({ enabled: true })), true);
  assert.equal(isImLearningDirty(saved, config({ since_ms: NOW })), true);
  assert.equal(isImLearningDirty(saved, config({ fetch_interval_seconds: 900 })), true);
  assert.equal(isImLearningDirty(saved, config({ fetch_top_n: 100 })), true);
  assert.equal(
    isImLearningDirty(
      saved,
      config({ targets: [{ channel_id: 'welink', kind: 'group', external_id: 'G2' }] }),
    ),
    true,
  );
});

test('mergeTargets 按三元组去重，已存在条目（含 title）优先', () => {
  const existing = [{ channel_id: 'welink', kind: 'group', external_id: 'G1', title: '旧标题' }];
  const merged = mergeTargets(existing, [
    { channel_id: 'welink', kind: 'group', external_id: 'G1', title: '新标题' },
    { channel_id: 'feishu', kind: 'user', external_id: 'U1', title: null },
  ]);
  assert.equal(merged.length, 2);
  assert.equal(merged[0].title, '旧标题');
  assert.equal(merged[1].external_id, 'U1');
});

test('markDiscoveredItems 按三元组标记「学习中」，与 kind/external_id 对齐', () => {
  const targets = [
    { channel_id: 'welink', kind: 'group', external_id: 'G1' },
    { channel_id: 'feishu', kind: 'user', external_id: 'U1' },
  ];
  const marked = markDiscoveredItems(
    [
      { channel_id: 'welink', target_kind: 'group', external_id: 'G1', title: '项目群' },
      { channel_id: 'welink', target_kind: 'group', external_id: 'G2', title: '别的群' },
      { channel_id: 'feishu', target_kind: 'user', external_id: 'U1' },
      // 同 external_id 但 kind 不同 → 不算已在学。
      { channel_id: 'welink', target_kind: 'user', external_id: 'G1' },
    ],
    targets,
  );
  assert.deepEqual(marked.map((item) => item.learning), [true, false, true, false]);

  // discoverItemToTarget：target_kind → kind 字段名转换。
  assert.deepEqual(discoverItemToTarget(marked[0]), {
    channel_id: 'welink',
    kind: 'group',
    external_id: 'G1',
    title: '项目群',
  });
});

test('parseBulkExternalIds：空行忽略、去重、含空白行记行号', () => {
  const text = ['G1', '', '  G2  ', 'G1', 'bad id with space', 'G3'].join('\n');
  const { targets, invalidLines } = parseBulkExternalIds(text, 'welink', 'group');
  assert.deepEqual(
    targets.map((target) => target.external_id),
    ['G1', 'G2', 'G3'],
  );
  assert.ok(targets.every((target) => target.channel_id === 'welink' && target.kind === 'group'));
  assert.deepEqual(invalidLines, [5]);

  assert.deepEqual(parseBulkExternalIds('', 'welink', 'user'), {
    targets: [],
    invalidLines: [],
  });
});

test('validateImLearningDraft 对齐后端校验规则', () => {
  // 合法：未启用 + 空 targets（默认形态）。
  assert.equal(validateImLearningDraft(config()), null);
  // 合法：启用 + 有 targets + 边界值。
  assert.equal(
    validateImLearningDraft(
      config({
        enabled: true,
        fetch_interval_seconds: IM_LEARNING_INTERVAL_MAX_SECONDS,
        fetch_top_n: 1,
      }),
    ),
    null,
  );

  // enabled=true 无 targets。
  assert.equal(
    validateImLearningDraft(config({ enabled: true, targets: [] })),
    'targetsRequired',
  );
  // 三元组重复。
  assert.equal(
    validateImLearningDraft(
      config({
        targets: [
          { channel_id: 'welink', kind: 'group', external_id: 'G1' },
          { channel_id: 'welink', kind: 'group', external_id: 'G1', title: '重复' },
        ],
      }),
    ),
    'targetsDuplicated',
  );
  // 周期越界（gt=0 / le=31536000）。
  assert.equal(validateImLearningDraft(config({ fetch_interval_seconds: 0 })), 'intervalRange');
  assert.equal(
    validateImLearningDraft(config({ fetch_interval_seconds: IM_LEARNING_INTERVAL_MAX_SECONDS + 1 })),
    'intervalRange',
  );
  // topN 越界（ge=1 / le=200）与整数。
  assert.equal(validateImLearningDraft(config({ fetch_top_n: 0 })), 'topNRange');
  assert.equal(validateImLearningDraft(config({ fetch_top_n: 201 })), 'topNRange');
  assert.equal(validateImLearningDraft(config({ fetch_top_n: 50.5 })), 'topNRange');
  // since_ms 负值。
  assert.equal(validateImLearningDraft(config({ since_ms: -1 })), 'sinceInvalid');
});
