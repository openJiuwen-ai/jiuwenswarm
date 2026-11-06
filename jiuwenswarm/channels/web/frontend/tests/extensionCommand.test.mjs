import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  canUseSlashCommand,
  findSlashCommand,
  i18n,
  parseExtensionSlashArgs,
  parseSlashLine,
  runExtensionCommand,
  shouldExecuteRegisteredSlashCommand,
  useChatStore,
  useSessionStore,
  webClient,
} from '../node_modules/.cache/extension-command/commands.mjs';

const translations = Object.fromEntries(['zh', 'en'].map((language) => [
  language,
  JSON.parse(readFileSync(new URL(`../src/i18n/locales/${language}.json`, import.meta.url), 'utf8')),
]));

function translate(language = 'zh') {
  return (key, values = {}) => {
    const value = key.split('.').reduce((current, part) => current?.[part], translations[language]);
    assert.equal(typeof value, 'string', `Missing ${language} translation: ${key}`);
    return value.replace(/{{(\w+)}}/g, (_, name) => String(values[name]));
  };
}

function successfulReport(packageId = 'demo.pkg', generation = 1) {
  return { package_id: packageId, status: 'SUCCESS', current_generation: generation };
}

function requestStub(result) {
  const calls = [];
  return {
    calls,
    request: async (...args) => {
      calls.push(args);
      return result;
    },
  };
}

const windowsPath = String.raw`D:\extension packages\示例.zip`;
const installedPath = String.raw`C:\Users\admin\.jiuwenswarm\extension_pkgs\demo.pkg\1.0.0`;

for (const [args, expected] of [
  ['', { action: 'help' }],
  [' query ', { action: 'query' }],
  ['QUERY Demo.Pkg', { action: 'query', packageId: 'Demo.Pkg' }],
  ['uninstall demo.pkg', { action: 'uninstall', packageId: 'demo.pkg' }],
  ['reload demo.pkg', { action: 'reload', packageId: 'demo.pkg' }],
  [`install "${windowsPath}"`, { action: 'install', path: windowsPath }],
  [`install ${windowsPath}`, { action: 'install', path: windowsPath }],
  ['install D:\\packages\\demo.ZIP', { action: 'install', path: 'D:\\packages\\demo.ZIP' }],
  [String.raw`install \\server\share\demo.zip`, { action: 'install', path: String.raw`\\server\share\demo.zip` }],
  ["install '/opt/extension packages/demo.zip'", { action: 'install', path: '/opt/extension packages/demo.zip' }],
  ['install /opt/extension packages/demo.zip', { action: 'install', path: '/opt/extension packages/demo.zip' }],
  ['install /opt/extensions/demo.zip', { action: 'install', path: '/opt/extensions/demo.zip' }],
]) {
  test(`parse: ${args || '<help>'}`, () => {
    assert.deepEqual(parseExtensionSlashArgs(args), expected);
  });
}

for (const args of [
  'install', 'uninstall', 'reload', 'unknown demo.pkg', 'query one two', 'reload one two',
  'install relative/path', 'install ~/demo.zip', 'install D:relative',
  'install /one /two', 'install ""', 'install "   "', 'install "D:\\demo',
  'install "/one" "/two"', 'query\nreload demo.pkg', 'reload "one two"',
  'install D:\\packages\\demo\\', 'install /opt/demo', 'install /opt/demo.zip/',
  'install /opt/demo.txt', 'reload demo..pkg', 'query /demo', `uninstall ${'a'.repeat(129)}`,
  'install /one\r\n/extension uninstall demo.pkg',
]) {
  test(`reject invalid arguments without any request: ${JSON.stringify(args)}`, async () => {
    assert.equal(parseExtensionSlashArgs(args), null);
    const stub = requestStub({});
    const output = await runExtensionCommand(args, 'session-1', stub.request, translate());
    assert.match(output, /(?:参数有误|ZIP|package_id)/);
    assert.match(output, /\/extension install/);
    assert.equal(stub.calls.length, 0);
  });
}

test('bare command displays help without creating a request', async () => {
  const stub = requestStub({});
  const output = await runExtensionCommand('', 'session-1', stub.request, translate());
  for (const name of ['install', 'query', 'uninstall', 'reload']) assert.ok(output.includes(`/extension ${name}`));
  assert.match(output, /ZIP/);
  assert.match(output, /package_id/);
  assert.doesNotMatch(output, /AgentServer|所有会话|加引号|代次/);
  assert.equal(stub.calls.length, 0);
});

for (const language of ['zh', 'en']) {
  test(`install shows package_id, directory and reload instruction (${language})`, async () => {
    const stub = requestStub({
      reports: [successfulReport()],
      installed_packages: [{
        package_id: 'demo.pkg', version: '1.0.0', install_path: installedPath, generation: 1,
      }],
    });
    const output = await runExtensionCommand(
      `install "${windowsPath}"`, 'session-1', stub.request, translate(language),
    );
    assert.deepEqual(stub.calls, [[
      'extensions.v2.install', { session_id: 'session-1', paths: [windowsPath] }, { timeoutMs: 330000 },
    ]]);
    assert.ok(output.includes(installedPath));
    assert.match(output, /package_id[：:]\s*demo.pkg/);
    assert.doesNotMatch(output, /AgentServer|生效代次|[Gg]eneration/);
    assert.ok(output.includes('1.0.0'));
    assert.ok(output.includes('/extension reload demo.pkg'));
    assert.ok(!output.includes(windowsPath));
  });
}

test('missing or mismatched installation metadata is never used to invent a path', async () => {
  for (const installed_packages of [undefined, [{
    package_id: 'demo.pkg', version: 'old', install_path: 'wrong path', generation: 0,
  }]]) {
    const stub = requestStub({ reports: [successfulReport()], installed_packages });
    const output = await runExtensionCommand('install /opt/demo.zip', 'session', stub.request, translate());
    assert.match(output, /安装成功/);
    assert.match(output, /未取得对应安装目录/);
    assert.match(output, /\/extension query/);
    assert.ok(!output.includes('wrong path'));
    assert.ok(!output.includes('/extension reload'));
  }
});

test('query supports all packages and one package, with active and inactive directories', async () => {
  const state = {
    active: [{ package_id: 'demo.pkg', version: '1.0.0', root: installedPath, generation: 2 }],
    installed: [installedPath, '/store/inactive.pkg/2.0.0'],
    installed_packages: [{
      package_id: 'inactive.pkg', version: '2.0.0', install_path: '/store/inactive.pkg/2.0.0',
    }],
  };
  for (const [args, params] of [
    ['query', { session_id: 'session' }],
    ['query demo.pkg', { session_id: 'session', package_id: 'demo.pkg' }],
  ]) {
    const stub = requestStub({ state });
    const output = await runExtensionCommand(args, 'session', stub.request, translate());
    assert.deepEqual(stub.calls[0].slice(0, 2), ['extensions.v2.query', params]);
    assert.ok(output.includes(installedPath));
    assert.match(output, /package_id：demo.pkg/);
    assert.match(output, /package_id：inactive.pkg/);
    assert.match(output, /版本：2.0.0/);
    assert.match(output, /运行状态：已生效/);
    assert.match(output, /运行状态：已安装，未生效/);
    assert.match(output, /安装目录：\/store\/inactive.pkg\/2.0.0/);
    assert.doesNotMatch(output, /AgentServer|代次|generation/);
    assert.equal(output.split(installedPath).length - 1, 1);
  }
});

test('empty query and failed query are displayed differently', async () => {
  const empty = await runExtensionCommand('query', 'session', async () => ({
    state: { active: [], installed: [] },
  }), translate());
  assert.match(empty, /未查询到扩展包/);
  const failed = await runExtensionCommand('query', 'session', async () => {
    throw new Error('query service failed');
  }, translate());
  assert.match(failed, /请求失败/);
  assert.match(failed, /query service failed/);
  assert.ok(!failed.includes('未查询到扩展包'));
});

for (const action of ['uninstall', 'reload']) {
  test(`${action} maps to the correct API and displays its report`, async () => {
    const stub = requestStub({ reports: [successfulReport('demo.pkg', 3)] });
    const output = await runExtensionCommand(`${action} demo.pkg`, 'session', stub.request, translate());
    assert.deepEqual(stub.calls[0].slice(0, 2), [
      `extensions.v2.${action}`, { session_id: 'session', package_id: 'demo.pkg' },
    ]);
    assert.match(output, /成功/);
    assert.match(output, /package_id：demo.pkg/);
    assert.doesNotMatch(output, /AgentServer|代次|generation/);
    assert.equal(stub.calls.length, 1);
  });
}

for (const action of ['install', 'uninstall', 'reload']) {
  test(`${action} displays FAILED reports even when the RPC itself succeeded`, async () => {
    const stub = requestStub({
      reports: [{
        package_id: 'demo.pkg', status: 'FAILED', current_generation: 1,
        error: { code: 'EXTENSION_LOAD_FAILED', message: 'bad replacement' },
      }],
      installed_packages: [{
        package_id: 'demo.pkg', install_path: 'old installed directory', version: '1.0.0', generation: 1,
      }],
    });
    const args = action === 'install' ? 'install /opt/demo.zip' : `${action} demo.pkg`;
    const output = await runExtensionCommand(args, 'session', stub.request, translate());
    assert.match(output, /失败/);
    assert.match(output, /package_id：demo.pkg/);
    assert.match(output, /EXTENSION_LOAD_FAILED: bad replacement/);
    assert.ok(!output.includes('成功'));
    assert.ok(!output.includes('old installed directory'));
    assert.ok(!output.includes('/extension reload'));
    assert.equal(stub.calls.length, 1);
  });
}

test('unknown outcome shows reconciliation facts without declaring success or retrying', async () => {
  let calls = 0;
  const output = await runExtensionCommand('reload demo.pkg', 'session', async () => {
    calls++;
    throw Object.assign(new Error('lost report'), {
      code: 'INTERNAL_ERROR',
      payload: {
        outcome: 'unknown',
        state: {
          active: [{ package_id: 'demo.pkg', version: '1.0.0', root: installedPath, generation: 2 }],
          installed: [installedPath],
        },
      },
    });
  }, translate());
  assert.match(output, /结果未确认/);
  assert.match(output, /不代表原操作成功/);
  assert.ok(output.includes(installedPath));
  assert.doesNotMatch(output, /AgentServer|代次|generation/);
  assert.ok(output.includes('/extension query demo.pkg'));
  assert.equal(calls, 1);
});

test('failed reconciliation retains both errors and never fabricates an empty state', async () => {
  const output = await runExtensionCommand('install /opt/demo.zip', 'session', async () => {
    throw Object.assign(new Error('original operation error'), {
      code: 'INTERNAL_ERROR', payload: { outcome: 'unknown', query_error: 'cannot query' },
    });
  }, translate());
  assert.match(output, /original operation error/);
  assert.match(output, /状态核对失败：cannot query/);
  assert.ok(!output.includes('未查询到扩展包'));
});

for (const code of ['REQUEST_TIMEOUT', 'AGENT_SERVER_TIMEOUT', 'WS_DISCONNECTED', 'WS_CLOSED']) {
  test(`${code} is unconfirmed for writes and does not cause a retry`, async () => {
    let calls = 0;
    const output = await runExtensionCommand('reload demo.pkg', 'session', async () => {
      calls++;
      throw Object.assign(new Error('AgentServer connection error'), { code });
    }, translate());
    assert.match(output, /结果未确认/);
    assert.ok(output.includes('/extension query demo.pkg'));
    assert.doesNotMatch(output, /AgentServer|AGENT_SERVER|WS_DISCONNECTED|WS_CLOSED|REQUEST_TIMEOUT/);
    assert.equal(calls, 1);
  });
}

test('an unavailable service is explained without an internal process name', async () => {
  const output = await runExtensionCommand('query', 'session', async () => {
    throw Object.assign(new Error('AgentServer is unavailable'), { code: 'SERVICE_UNAVAILABLE' });
  }, translate());
  assert.match(output, /扩展服务/);
  assert.doesNotMatch(output, /AgentServer|SERVICE_UNAVAILABLE/);
});

test('a rejected ZIP source gets an actionable message before package loading', async () => {
  const stub = requestStub({});
  stub.request = async (...args) => {
    stub.calls.push(args);
    throw Object.assign(new Error('install accepts existing .zip files only'), { code: 'INVALID_PARAMS' });
  };
  const output = await runExtensionCommand('install /opt/missing.zip', 'session', stub.request, translate());
  assert.match(output, /检查 .zip 文件是否存在/);
  assert.match(output, /目录不能作为安装来源/);
  assert.doesNotMatch(output, /EXTENSION_LOAD_FAILED|结果未确认|INVALID_PARAMS/);
  assert.equal(stub.calls.length, 1);
});

test('missing reports never become a success message', async () => {
  const output = await runExtensionCommand('reload demo.pkg', 'session', async () => ({}), translate());
  assert.match(output, /结果尚未确认/);
  assert.ok(output.includes('/extension query demo.pkg'));
});

test('command registry uses the structured request and generic result messages without chat submission', async () => {
  await i18n.changeLanguage('en');
  const command = findSlashCommand('extension');
  assert.ok(command);
  assert.equal(canUseSlashCommand(command, false), true);
  assert.equal(canUseSlashCommand(command, true), true);
  assert.equal(shouldExecuteRegisteredSlashCommand('extension', 'query', 'team'), false);
  assert.deepEqual(parseSlashLine('/EXTENSION query demo.pkg'), { name: 'extension', args: 'query demo.pkg' });
  const messages = [];
  const calls = [];
  const originalRequest = webClient.request;
  webClient.request = async (...args) => {
    calls.push(args);
    return { state: { active: [], installed: [] } };
  };
  try {
    await command.execute({
      sessionId: 'session', mode: 'agent', inputLine: '/extension query',
      addMessage: (sessionId, message) => messages.push({ sessionId, ...message }),
      submitMessage: () => assert.fail('management command became a chat turn'),
    }, 'query');
  } finally {
    webClient.request = originalRequest;
  }
  assert.deepEqual(calls[0].slice(0, 2), ['extensions.v2.query', { session_id: 'session' }]);
  assert.equal(messages.length, 1);
  assert.match(messages[0].commandOutput, /No extension packages/);
  for (const message of messages) {
    assert.equal(message.sessionId, 'session');
    assert.equal(message.isCommandOutput, true);
    assert.equal(message.commandName, 'extension');
    assert.equal(message.role, 'system');
  }
});

for (const [args, method, params] of [
  ['', null, null],
  ['query', 'extensions.v2.query', {}],
  ['query demo.pkg', 'extensions.v2.query', { package_id: 'demo.pkg' }],
  ['install /opt/demo.zip', 'extensions.v2.install', { paths: ['/opt/demo.zip'] }],
  ['uninstall demo.pkg', 'extensions.v2.uninstall', { package_id: 'demo.pkg' }],
  ['reload demo.pkg', 'extensions.v2.reload', { package_id: 'demo.pkg' }],
]) {
  test(`a fresh conversation can execute /extension ${args} before its first chat message`, async () => {
    await i18n.changeLanguage('en');
    const command = findSlashCommand('extension');
    assert.equal(canUseSlashCommand(command, false), true);
    assert.equal(shouldExecuteRegisteredSlashCommand('extension', args, 'agent'), true);
    const previousSession = useChatStore.getState().activeSessionId;
    const previousSessions = useSessionStore.getState().sessions;
    useChatStore.getState().ensureRuntime('new');
    useChatStore.getState().setActiveSessionId('new');
    const calls = [];
    const originalRequest = webClient.request;
    webClient.request = async (...requestArgs) => {
      calls.push(requestArgs);
      return method === 'extensions.v2.query'
        ? { state: { active: [], installed: [] } }
        : { reports: [successfulReport()] };
    };
    try {
      await command.execute({
        sessionId: 'new', mode: 'agent', inputLine: `/extension${args ? ` ${args}` : ''}`,
        addMessage: useChatStore.getState().addMessage,
        submitMessage: () => assert.fail('extension management started a chat turn'),
      }, args);
      if (method) {
        assert.equal(calls.length, 1);
        assert.deepEqual(calls[0].slice(0, 2), [method, params]);
      } else {
        assert.equal(calls.length, 0);
      }
      const runtime = useChatStore.getState().getRuntime('new');
      assert.equal(runtime.messages.length, 1);
      assert.equal(runtime.taskQueue.length, 0);
      assert.equal(runtime.isProcessing, false);
      for (const message of runtime.messages) {
        assert.equal(message.isCommandOutput, true);
        assert.equal(message.commandName, 'extension');
        assert.equal(message.role, 'system');
      }
      assert.equal(useChatStore.getState().activeSessionId, 'new');
      assert.equal(useSessionStore.getState().sessions, previousSessions);
    } finally {
      webClient.request = originalRequest;
      useChatStore.getState().removeRuntime('new');
      useChatStore.getState().setActiveSessionId(previousSession);
    }
  });
}

for (const fail of [false, true]) {
  test(`a pending request emits no intermediate message and ends with one ${fail ? 'error' : 'result'}`, async () => {
    await i18n.changeLanguage('zh');
    const messages = [];
    let finish;
    let reject;
    const response = new Promise((resolve, rejectPromise) => {
      finish = resolve;
      reject = rejectPromise;
    });
    const originalRequest = webClient.request;
    let calls = 0;
    webClient.request = () => {
      calls++;
      return response;
    };
    try {
      const execution = findSlashCommand('extension').execute({
        sessionId: 'session', mode: 'agent', inputLine: '/extension query',
        addMessage: (_sessionId, message) => messages.push(message),
        submitMessage: () => assert.fail('management command became a chat turn'),
      }, 'query');
      assert.equal(calls, 1);
      assert.deepEqual(messages, []);
      if (fail) reject(new Error('query unavailable'));
      else finish({ state: { active: [], installed: [] } });
      await execution;
      assert.equal(messages.length, 1);
      assert.match(messages[0].commandOutput, fail ? /请求失败/ : /未查询到扩展包/);
      assert.doesNotMatch(messages[0].commandOutput, /正在处理|请稍候/);
      assert.equal(calls, 1);
    } finally {
      webClient.request = originalRequest;
    }
  });
}

test('all extension command messages have matching Chinese and English keys', () => {
  assert.deepEqual(
    Object.keys(translations.zh.extensionCommand).sort(),
    Object.keys(translations.en.extensionCommand).sort(),
  );
});

test('a newline after the main command cannot become a management request after shared parsing', async () => {
  await i18n.changeLanguage('en');
  const inputLine = '/extension\nuninstall demo.pkg';
  const parsed = parseSlashLine(inputLine);
  const command = findSlashCommand(parsed.name);
  const messages = [];
  const originalRequest = webClient.request;
  webClient.request = async () => assert.fail('multiline input reached the backend');
  try {
    await command.execute({
      sessionId: 'session', mode: 'agent', inputLine,
      addMessage: (_sessionId, message) => messages.push(message),
      submitMessage: () => assert.fail('multiline command became a chat turn'),
    }, parsed.args);
  } finally {
    webClient.request = originalRequest;
  }
  assert.equal(messages.length, 1);
  assert.match(messages[0].commandOutput, /one extension command per line/);
});


test('query without id displays report details from both domains', async () => {
  const stub = requestStub({ state: {
    active: [{ package_id: 'server.demo', version: '2.0.0', root: installedPath, generation: 2 }],
    installed: [installedPath],
    package_ids: ['gateway.demo', 'server.demo'],
    packages: [
      { package_id: 'server.demo', domain: 'agentserver', status: 'success', active_version: '2.0.0' },
      { package_id: 'gateway.demo', domain: 'gateway', status: 'failed', active_version: null },
    ],
  } });
  const output = await runExtensionCommand('query', 'session-1', stub.request, translate());
  assert.ok(output.includes('gateway.demo'));
  assert.ok(output.includes('server.demo'));
  assert.match(output, /域：gateway/);
  assert.match(output, /域：agentserver/);
  assert.match(output, /当前生效版本：2.0.0/);
  assert.match(output, /运行状态：已生效/);
  assert.ok(output.includes(installedPath));
  assert.doesNotMatch(output, /报告状态|当前进程运行状态/);
  assert.equal(output.split('当前生效版本：2.0.0').length - 1, 1);
});

test('query with id merges the report and local facts into one package block', async () => {
  const stub = requestStub({ state: {
    active: [{ package_id: 'demo.pkg', version: '2.3.4', root: installedPath }],
    installed: [installedPath],
    package_ids: ['demo.pkg'],
    packages: [{ package_id: 'demo.pkg', domain: 'agentserver', active_version: '2.3.4' }],
  } });
  const output = await runExtensionCommand('query demo.pkg', 'session-1', stub.request, translate());
  assert.ok(output.includes('2.3.4'));
  assert.ok(output.includes(installedPath));
  assert.match(output, /域：agentserver/);
  assert.match(output, /运行状态：已生效/);
  assert.equal(output.split('package_id：demo.pkg').length - 1, 1);
});
