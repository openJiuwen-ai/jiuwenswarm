import assert from 'node:assert/strict';
import test from 'node:test';

import {
  campaignBannerVariant,
  campaignErrorMessageKey,
  classifyCampaignError,
} from '../node_modules/.cache/free-model-campaign-error/campaignError.mjs';

test('only the real campaign-end marker renders the ended banner', () => {
  assert.equal(campaignBannerVariant('active'), 'active');
  assert.equal(campaignBannerVariant('ended'), 'ended');
  assert.equal(campaignBannerVariant('off'), 'off');
});

test('a config-fetch failure is an error banner, not the ended banner', () => {
  assert.equal(campaignBannerVariant('unavailable'), 'error');
});

test('certificate failures map to a certificate message', () => {
  for (const status of [495, 496, 497, 526]) {
    const error = classifyCampaignError({ status });
    assert.equal(error.kind, 'certificate');
    assert.equal(campaignErrorMessageKey(error), 'settingsPanel.freeModels.certificateError');
  }
});

test('unreachable or firewalled endpoints map to a network message', () => {
  for (const status of [0, 502, 503, 504]) {
    const error = classifyCampaignError({ status });
    assert.equal(error.kind, 'network');
    assert.equal(campaignErrorMessageKey(error), 'settingsPanel.freeModels.networkError');
  }
  const networkError = classifyCampaignError({ network: true, message: 'Failed to fetch' });
  assert.equal(networkError.kind, 'network');
  assert.equal(campaignErrorMessageKey(networkError), 'settingsPanel.freeModels.networkError');
});

test('other http failures keep the status code for the message', () => {
  const error = classifyCampaignError({ status: 401, code: 'http_401' });
  assert.equal(error.kind, 'http');
  assert.equal(error.status, 401);
  assert.equal(error.code, 'http_401');
  assert.equal(campaignErrorMessageKey(error), 'settingsPanel.freeModels.httpError');
});

test('an unavailable state without transport details falls back to config', () => {
  const error = classifyCampaignError({});
  assert.equal(error.kind, 'config');
  assert.equal(campaignErrorMessageKey(error), 'settingsPanel.freeModels.unavailableDescription');
});
