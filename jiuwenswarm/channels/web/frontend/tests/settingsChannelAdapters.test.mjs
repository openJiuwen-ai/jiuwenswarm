import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildDingtalkFormPayload,
  buildFeishuDeletionPayload,
  buildFeishuEnabledPayload,
  buildDiscordFormPayload,
  buildFeishuFormPayload,
  buildSingleChannelDeletionPayload,
  buildSlackFormPayload,
  buildTelegramFormPayload,
  buildWhatsAppFormPayload,
  buildXiaoyiFormPayload,
  channelConfigurationChecks,
  readDingtalkFormValues,
  readDiscordFormValues,
  readFeishuFormValues,
  readSlackFormValues,
  readTelegramFormValues,
  readWhatsAppFormValues,
  readXiaoyiFormValues,
} from '../node_modules/.cache/settings-refactor/modules/channels/channelAdapters.js';
import {
  CHANNEL_FIELD_REQUIREMENTS,
  createChannelFormRules,
  isFeishuAppConfigured,
  shouldConfirmXiaoyiEnable,
} from '../node_modules/.cache/settings-refactor/modules/channels/channelRequirements.js';
import { getSettingsChannelGuideUrl } from '../node_modules/.cache/settings-refactor/modules/channels/channelGuideUrls.js';

test('Xiaoyi enable confirmation is required only when enabling without api_id', () => {
  assert.equal(shouldConfirmXiaoyiEnable(true, ''), true);
  assert.equal(shouldConfirmXiaoyiEnable(true, '   '), true);
  assert.equal(shouldConfirmXiaoyiEnable(true, 'api-id'), false);
  assert.equal(shouldConfirmXiaoyiEnable(false, ''), false);
});

test('channel configuration guides resolve to versioned localized documentation', () => {
  const baseUrl = 'https://gitcode.com/openJiuwen/jiuwenswarm/blob/0.2.5/docs';
  const expectedPaths = {
    zh: {
      xiaoyi: 'zh/%E5%9B%BD%E5%86%85%E9%A2%91%E9%81%93.md#%E5%B0%8F%E8%89%BA',
      feishu: 'zh/%E5%9B%BD%E5%86%85%E9%A2%91%E9%81%93.md#%E9%A3%9E%E4%B9%A6',
      dingtalk: 'zh/%E5%9B%BD%E5%86%85%E9%A2%91%E9%81%93.md#%E9%92%89%E9%92%89',
      telegram: 'zh/%E6%B5%B7%E5%A4%96%E9%A2%91%E9%81%93.md#telegram',
      discord: 'zh/%E6%B5%B7%E5%A4%96%E9%A2%91%E9%81%93.md#discord',
      slack: 'zh/%E6%B5%B7%E5%A4%96%E9%A2%91%E9%81%93.md#slack',
      whatsapp: 'zh/%E6%B5%B7%E5%A4%96%E9%A2%91%E9%81%93.md#whatsapp',
    },
    en: {
      xiaoyi: 'en/ChinaChannels.md#xiaoyi',
      feishu: 'en/ChinaChannels.md#feishu-lark',
      dingtalk: 'en/ChinaChannels.md#dingtalk',
      telegram: 'en/InternationalChannels.md#telegram',
      discord: 'en/InternationalChannels.md#discord',
      slack: 'en/InternationalChannels.md#slack',
      whatsapp: 'en/InternationalChannels.md#whatsapp',
    },
  };

  for (const [language, channelPaths] of Object.entries(expectedPaths)) {
    for (const [channelId, path] of Object.entries(channelPaths)) {
      assert.equal(getSettingsChannelGuideUrl(channelId, language), `${baseUrl}/${path}`);
    }
  }
});

test('channel adapters preserve Feishu multi-account fields', () => {
  const values = readFeishuFormValues({
    apps: [
      {
        name: 'B',
        is_default: false,
        enabled: true,
        enable_streaming: false,
        app_id: ' app-b ',
        app_secret: ' secret ',
        encrypt_key: ' key ',
        verification_token: ' token ',
        chat_id: ' chat ',
        allow_from: [' user-1 ', '', 'user-2'],
        group_digital_avatar: true,
        my_user_id: ' me ',
        bot_name: ' bot ',
        enable_memory: true,
      },
      { name: 'A', is_default: true, app_id: 'app-a' },
    ],
  });
  assert.deepEqual(
    values.apps.map((app) => app.name),
    ['A', 'B'],
  );
  const payload = buildFeishuFormPayload(values);
  assert.deepEqual(payload.apps[1], {
    name: 'B',
    is_default: false,
    enabled: true,
    enable_streaming: false,
    app_id: 'app-b',
    app_secret: 'secret',
    encrypt_key: 'key',
    verification_token: 'token',
    chat_id: 'chat',
    allow_from: ['user-1', 'user-2'],
    group_digital_avatar: true,
    my_user_id: 'me',
    bot_name: 'bot',
    enable_memory: true,
  });
});

test('channel deletion payloads remove configuration instead of only disabling it', () => {
  const feishuValues = readFeishuFormValues({
    apps: [
      { name: 'primary', is_default: true, enabled: true, app_id: 'first', app_secret: 'secret-1' },
      { name: 'secondary', is_default: false, enabled: true, app_id: 'second', app_secret: 'secret-2' },
    ],
  });
  assert.deepEqual(buildFeishuDeletionPayload(feishuValues, 0), {
    apps: [
      {
        name: 'secondary',
        is_default: true,
        enabled: true,
        enable_streaming: true,
        app_id: 'second',
        app_secret: 'secret-2',
        encrypt_key: '',
        verification_token: '',
        chat_id: '',
        allow_from: [],
        group_digital_avatar: false,
        my_user_id: '',
        bot_name: '',
        enable_memory: false,
      },
    ],
  });
  assert.deepEqual(buildFeishuDeletionPayload(feishuValues, 1).apps.length, 1);
  assert.throws(() => buildFeishuDeletionPayload(feishuValues, 2), /Invalid Feishu account index/);
  assert.deepEqual(buildSingleChannelDeletionPayload('xiaoyi'), { apps: [] });
  assert.deepEqual(buildSingleChannelDeletionPayload('dingtalk'), {
    enabled: false,
    client_id: '',
    client_secret: '',
    allow_from: [],
  });
  assert.deepEqual(buildSingleChannelDeletionPayload('telegram'), {
    enabled: false,
    bot_token: '',
    allow_from: [],
    parse_mode: 'Markdown',
    group_chat_mode: 'mention',
  });
  assert.deepEqual(buildSingleChannelDeletionPayload('discord'), {
    enabled: false,
    bot_token: '',
    application_id: '',
    guild_id: '',
    channel_id: '',
    block_dm: false,
    allow_from: [],
  });
  assert.deepEqual(buildSingleChannelDeletionPayload('slack'), {
    enabled: false,
    bot_token: '',
    app_token: '',
    allow_from: [],
    allowed_channel_ids: [],
    default_channel_id: '',
    reply_in_thread: true,
  });
  assert.deepEqual(buildSingleChannelDeletionPayload('whatsapp'), {
    enabled: false,
    bridge_ws_url: '',
    default_jid: '',
    allow_from: [],
    enable_streaming: true,
    auto_start_bridge: false,
    bridge_command: '',
    bridge_workdir: '',
  });
});

test('channel enabled payload updates only the selected Feishu application', () => {
  const values = readFeishuFormValues({
    apps: [
      { name: 'A', app_id: 'a', app_secret: 'secret-a', enabled: true },
      { name: 'B', app_id: 'b', app_secret: 'secret-b', enabled: false },
    ],
  });
  const payload = buildFeishuEnabledPayload(values, 1, true);
  assert.equal(payload.apps[0].enabled, true);
  assert.equal(payload.apps[1].enabled, true);
  assert.equal(payload.apps[0].app_secret, 'secret-a');
  assert.equal(payload.apps[1].app_secret, 'secret-b');
  assert.throws(() => buildFeishuEnabledPayload(values, 2, true), /Invalid Feishu account index/);
});

test('configured channel checks distinguish saved disabled configurations from empty defaults', () => {
  assert.equal(channelConfigurationChecks.feishu({ apps: [] }), false);
  assert.equal(channelConfigurationChecks.feishu({ apps: [{ app_id: 'app' }] }), false);
  assert.equal(channelConfigurationChecks.feishu({ apps: [{ app_id: 'app', app_secret: 'secret' }] }), true);
  assert.equal(channelConfigurationChecks.xiaoyi({ apps: [{ enabled: false }] }), false);
  assert.equal(channelConfigurationChecks.xiaoyi({ apps: [{ ak: 'ak', sk: 'sk', agent_id: 'agent' }] }), true);
  assert.equal(channelConfigurationChecks.dingtalk({ client_id: 'id' }), false);
  assert.equal(channelConfigurationChecks.dingtalk({ client_id: 'id', client_secret: 'secret', enabled: false }), true);
  assert.equal(channelConfigurationChecks.telegram({ bot_token: ' ' }), false);
  assert.equal(channelConfigurationChecks.telegram({ bot_token: 'token', enabled: false }), true);
  assert.equal(channelConfigurationChecks.discord({ bot_token: '' }), false);
  assert.equal(channelConfigurationChecks.discord({ bot_token: 'token', enabled: false }), true);
  assert.equal(channelConfigurationChecks.slack({ bot_token: 'bot' }), false);
  assert.equal(channelConfigurationChecks.slack({ bot_token: 'bot', app_token: 'app', enabled: false }), true);
  assert.equal(channelConfigurationChecks.whatsapp({ bridge_ws_url: 'ws://bridge', enabled: false }), true);
  assert.equal(channelConfigurationChecks.whatsapp({ bridge_ws_url: '', enabled: false }), false);
  assert.equal(isFeishuAppConfigured({ app_id: 'app', app_secret: 'secret' }), true);
  assert.equal(isFeishuAppConfigured({ app_id: 'app', app_secret: ' ' }), false);
});

test('channel business requirements drive required labels, validation, and configured checks', () => {
  const requiredFields = Object.fromEntries(
    Object.entries(CHANNEL_FIELD_REQUIREMENTS).map(([channelId, fields]) => [
      channelId,
      Object.entries(fields)
        .filter(([, requirement]) => requirement === 'required')
        .map(([field]) => field),
    ]),
  );
  assert.deepEqual(requiredFields, {
    xiaoyi: ['ak', 'sk', 'agent_id'],
    feishu: ['app_id', 'app_secret'],
    dingtalk: ['client_id', 'client_secret'],
    telegram: ['bot_token'],
    discord: ['bot_token'],
    slack: ['bot_token', 'app_token'],
    whatsapp: ['bridge_ws_url'],
  });

  for (const [channelId, fields] of Object.entries(requiredFields)) {
    const rules = createChannelFormRules(channelId, 'required');
    assert.deepEqual(Object.keys(rules), fields);
    for (const field of fields) {
      const validator = rules[field][0].validator;
      assert.equal(validator(' ', {}), 'required');
      assert.equal(validator('configured', {}), undefined);
    }
  }
});

test('channel adapters preserve Xiaoyi default-account payload shape', () => {
  const values = readXiaoyiFormValues({
    apps: [
      { name: 'secondary', is_default: false, ak: 'other' },
      { name: 'primary', is_default: true, enabled: true, ak: ' ak ', sk: ' sk ', api_id: ' api ' },
    ],
  });
  assert.equal(values.name, 'primary');
  assert.deepEqual(buildXiaoyiFormPayload(values), {
    apps: [
      {
        name: 'primary',
        is_default: true,
        enabled: true,
        ak: 'ak',
        sk: 'sk',
        agent_id: '',
        api_id: 'api',
        enable_streaming: true,
      },
    ],
  });
});

test('simple channel adapters round-trip list and boolean fields', () => {
  assert.deepEqual(
    buildDingtalkFormPayload(
      readDingtalkFormValues({
        enabled: true,
        client_id: ' id ',
        client_secret: ' secret ',
        allow_from: ['one', 'two'],
      }),
    ),
    {
      enabled: true,
      client_id: 'id',
      client_secret: 'secret',
      allow_from: ['one', 'two'],
    },
  );

  assert.deepEqual(
    buildTelegramFormPayload(
      readTelegramFormValues({
        enabled: true,
        bot_token: ' token ',
        allow_from: ['1', '2'],
        parse_mode: 'HTML',
        group_chat_mode: 'reply',
      }),
    ),
    {
      enabled: true,
      bot_token: 'token',
      allow_from: ['1', '2'],
      parse_mode: 'HTML',
      group_chat_mode: 'reply',
    },
  );

  assert.deepEqual(
    buildDiscordFormPayload(
      readDiscordFormValues({
        enabled: true,
        bot_token: ' token ',
        application_id: ' app ',
        guild_id: ' guild ',
        channel_id: ' channel ',
        block_dm: '1',
        allow_from: ['user'],
      }),
    ),
    {
      enabled: true,
      bot_token: 'token',
      application_id: 'app',
      guild_id: 'guild',
      channel_id: 'channel',
      block_dm: true,
      allow_from: ['user'],
    },
  );

  assert.deepEqual(
    buildSlackFormPayload(
      readSlackFormValues({
        bot_token: ' bot ',
        app_token: ' app ',
        allow_from: ['user'],
        allowed_channel_ids: ['room'],
        default_channel_id: ' default ',
        reply_in_thread: false,
      }),
    ),
    {
      enabled: false,
      bot_token: 'bot',
      app_token: 'app',
      allow_from: ['user'],
      allowed_channel_ids: ['room'],
      default_channel_id: 'default',
      reply_in_thread: false,
    },
  );

  assert.deepEqual(
    buildWhatsAppFormPayload(
      readWhatsAppFormValues({
        enabled: true,
        bridge_ws_url: ' ws://bridge ',
        default_jid: ' jid ',
        allow_from: ['one'],
        enable_streaming: false,
        auto_start_bridge: true,
        bridge_command: ' command ',
        bridge_workdir: ' dir ',
      }),
    ),
    {
      enabled: true,
      bridge_ws_url: 'ws://bridge',
      default_jid: 'jid',
      allow_from: ['one'],
      enable_streaming: false,
      auto_start_bridge: true,
      bridge_command: 'command',
      bridge_workdir: 'dir',
    },
  );
});
