/**
 * 授权卡选项语义匹配与回答装配。
 *
 * 覆盖缺陷单 #5910 的前端半边：选项取值可能写成后端下划线 id（allow_once）、
 * ACP 连字符 id（allow-once）或中英文展示名，三套写法都要归到同一语义；
 * 点击「总是允许」绝不能被换成「本次允许」提交。
 *
 * 词表不变量只对 permission / confirm 路径成立：后端
 * permission_options._ALIAS_SOURCES 是归一化查表；skill evolution 审批
 * （interface.py 的 action_by_value）仍是不归一化的字面量表，不在本条契约内，
 * 它下发的取值就是 allow_once / allow_always / reject 本身。
 * 该别名表由用例运行时从后端源码读出（python3），不在前端复制一份。
 */
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

// 词表不变量直接读后端源码，不在这里抄一份：后端增删别名时这条用例会跟着变。
const PERMISSION_OPTIONS_PY = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../../../../../jiuwenswarm/agents/harness/common/rails/interrupt/permission_options.py',
);

function readBackendAliases() {
  const script = [
    'import importlib.util, json, sys',
    'spec = importlib.util.spec_from_file_location("permission_options", sys.argv[1])',
    'module = importlib.util.module_from_spec(spec)',
    'spec.loader.exec_module(module)',
    'print(json.dumps({',
    '    action: [module.normalize_option_value(alias) for alias in aliases]',
    '    for action, aliases in module._ALIAS_SOURCES',
    '}))',
  ].join('\n');
  const output = execFileSync('python3', ['-I', '-c', script, PERMISSION_OPTIONS_PY], {
    encoding: 'utf8',
  });
  return JSON.parse(output);
}

const {
  ACTION_ORDER,
  buildAuthorizationAnswers,
  canApplyAuthorizationAction,
  classifyAuthOption,
  classifyPrompt,
  normalizeAuthOption,
  resolveAuthorizationActions,
} = await import(
  '../node_modules/.cache/authorization-prompt-routing/components/InteractionSlot/promptRouting.js'
);

const option = (label, value) => (value === undefined ? { label } : { label, value });
const question = (options, extra = {}) => ({
  question: `q-${options.map((item) => item.label).join('/')}`,
  header: 'h',
  options,
  ...extra,
});
const actionsFor = (questions) => resolveAuthorizationActions(questions);
const pickAction = (questions, label) => {
  const action = actionsFor(questions).find((item) => item.label === label);
  assert.ok(action, `action ${label} not found`);
  return action;
};
const selected = (questions, picked, smart = false) =>
  buildAuthorizationAnswers(questions, picked, smart)?.map((answer) => answer.selected_options[0]);

test('separator, case and language variants all resolve to one semantic', () => {
  const variants = {
    'allow-once': [
      'allow_once', 'allow-once', 'ALLOW_ONCE', '  Allow-Once  ', 'Allow Once', 'allow once',
      'approve', 'accept', 'proceed', '本次允许', '批准', '开始执行', '接收', '接受', '激活',
    ],
    'allow-always': ['always_allow', 'allow_always', 'allow-always', 'Always Allow', 'ALWAYS ALLOW', '永久记住', '总是允许'],
    'session-allow': ['session_allow', 'session-allow', 'Session Allow', '会话内记住'],
    reject: ['reject', 'reject_once', 'reject-once', 'Reject', '拒绝'],
  };
  for (const [semantic, values] of Object.entries(variants)) {
    for (const value of values) {
      assert.equal(classifyAuthOption(value), semantic, `${value} should be ${semantic}`);
    }
  }
  for (const unknown of ['', '   ', 'skip', '跳过', '确定', 'plan_execute', '继续规划']) {
    assert.equal(classifyAuthOption(unknown), 'other', `${unknown} should stay unrecognised`);
  }
  assert.equal(normalizeAuthOption('  Always-Allow  '), 'always_allow');
  assert.equal(normalizeAuthOption('  本次允许 '), '本次允许');
});

test('every permission vocabulary alias decodes to the same action family', (t) => {
  // 别名表在后端 permission_options._ALIAS_SOURCES 里读，不在这里复制：后端新增
  // 别名而前端没跟上时，这条会红。计划审批 legacy 取值（继续规划 / 其他意见 /
  // keep_planning）只出现在 InlineQuestionCard 的旧卡上，授权卡不渲染它们。
  const actionSemantics = {
    allow_once: 'allow-once',
    session_allow: 'session-allow',
    always_allow: 'allow-always',
    reject: 'reject',
  };
  const planLegacyAliases = new Set(['继续规划', '其他意见', 'keep_planning']);

  let backendAliases;
  try {
    backendAliases = readBackendAliases();
  } catch (error) {
    t.skip(`cannot read ${PERMISSION_OPTIONS_PY}: ${error.message}`);
    return;
  }

  assert.ok(Object.keys(backendAliases).length > 0, 'backend alias table is empty');
  for (const [action, aliases] of Object.entries(backendAliases)) {
    const semantic = actionSemantics[action];
    assert.ok(semantic, `backend action ${action} has no frontend semantic — extend this case`);
    for (const alias of aliases) {
      if (planLegacyAliases.has(alias)) continue;
      assert.equal(classifyAuthOption(alias), semantic, `${alias} (${action}) must be ${semantic}`);
    }
  }
});

test('clicking always-allow on a hyphenated card submits always-allow', () => {
  const card = [
    question([
      option('本次允许', 'allow-once'),
      option('总是允许', 'allow-always'),
      option('拒绝', 'reject'),
    ]),
  ];
  assert.deepEqual(selected(card, pickAction(card, '总是允许')), ['allow-always']);
  assert.deepEqual(selected(card, pickAction(card, '本次允许')), ['allow-once']);
  assert.deepEqual(selected(card, pickAction(card, '拒绝')), ['reject']);
});

test('a sibling option never stands in for the option the user clicked', () => {
  const card = [question([option('本次允许', 'allow_once'), option('接收', '接收')])];
  assert.equal(classifyAuthOption('allow_once'), classifyAuthOption('接收'));
  assert.deepEqual(selected(card, pickAction(card, '接收')), ['接收']);
  assert.deepEqual(selected(card, pickAction(card, '本次允许')), ['allow_once']);

  const unrecognised = [question([option('第一条'), option('第二条')])];
  assert.deepEqual(selected(unrecognised, pickAction(unrecognised, '第二条')), ['第二条']);
});

test('hyphenated four-option card keeps the canonical button order', () => {
  const card = [
    question([
      option('本次允许', 'allow-once'),
      option('会话内记住', 'session-allow'),
      option('永久记住', 'allow-always'),
      option('拒绝', 'reject'),
    ]),
  ];
  const labels = actionsFor(card).map((action) => action.label);
  assert.deepEqual(labels, ['拒绝', '永久记住', '会话内记住', '本次允许']);
  assert.deepEqual(actionsFor(card).map((action) => action.semantic), ACTION_ORDER);
});

test('an action that cannot reach every card is blocked instead of falling back', () => {
  const cards = [
    question([option('本次允许', 'allow_once'), option('拒绝', 'reject')]),
    question([option('执行', 'plan_execute'), option('跳过', 'plan_skip')]),
  ];
  const picked = pickAction(cards, '本次允许');
  assert.equal(canApplyAuthorizationAction(cards, picked), false);
  assert.equal(buildAuthorizationAnswers(cards, picked), null);
});

test('smart mode still falls back to the card reject option and stays clickable', () => {
  const cards = [
    question([option('本次允许', 'allow_once'), option('拒绝', 'reject')]),
    question([option('执行', 'plan_execute'), option('跳过', 'plan_skip')]),
  ];
  const picked = pickAction(cards, '本次允许');
  assert.equal(canApplyAuthorizationAction(cards, picked, true), true);
  assert.deepEqual(selected(cards, picked, true), ['allow_once', 'reject']);

  const withReject = [
    question([option('本次允许', 'allow_once')]),
    question([option('执行', 'plan_execute'), option('拒绝', 'reject')]),
  ];
  assert.deepEqual(selected(withReject, pickAction(withReject, '本次允许'), true), ['allow_once', 'reject']);
});

test('label-only activate cards submit the label the user clicked', () => {
  const card = [question([option('激活'), option('拒绝')])];
  assert.deepEqual(selected(card, pickAction(card, '拒绝')), ['拒绝']);
  assert.deepEqual(selected(card, pickAction(card, '激活')), ['激活']);
});

test('answers only ever echo a value the payload already carries', () => {
  const card = [
    question([
      option('本次允许', 'allow-once'),
      option('会话内记住', 'session-allow'),
      option('永久记住', 'allow-always'),
      option('拒绝', 'reject'),
    ]),
  ];
  for (const action of actionsFor(card)) {
    const answers = buildAuthorizationAnswers(card, action);
    assert.ok(answers);
    for (const answer of answers) {
      for (const value of answer.selected_options) {
        assert.ok(
          card[0].options.some((item) => (item.value || item.label) === value),
          `${value} is not one of the payload values`,
        );
      }
    }
  }
});

test('source-less permission cards route to the authorization slot', () => {
  const payload = (options) => ({ request_id: 'r', questions: [question(options)] });
  // 这两条是「词表合一」的判别用例：旧实现在这里返回 interaction（旧白名单只有
  // 本次允许/总是允许/永久记住/会话内记住/拒绝 + 下划线 id），退回字面量表即变红。
  assert.equal(
    classifyPrompt(payload([option('Allow once', 'allow-once'), option('Always allow', 'allow-always')])),
    'authorization',
  );
  assert.equal(classifyPrompt(payload([option('接收', '接受'), option('稍后')])), 'authorization');
  assert.equal(classifyPrompt(payload([option('你想先做哪件事？'), option('随便聊聊')])), 'interaction');
});
