import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const skillPanelSource = readFileSync(new URL('../src/components/SkillPanel/index.tsx', import.meta.url), 'utf8');

function sourceBetween(start, end) {
  const startIndex = skillPanelSource.indexOf(start);
  const endIndex = skillPanelSource.indexOf(end, startIndex);
  assert.notEqual(startIndex, -1, `missing source marker: ${start}`);
  assert.notEqual(endIndex, -1, `missing source marker: ${end}`);
  return skillPanelSource.slice(startIndex, endIndex);
}

test('Skill Hub marketplace and installation rely on the server-configured Hub', () => {
  const marketplaceSource = sourceBetween(
    'const fetchHubRecommendByType = useCallback',
    'const fetchOnlineSearch = useCallback',
  );
  const installationSource = sourceBetween(
    'const handleInstallHubSkill = useCallback',
    'const fetchSkillVersions = useCallback',
  );

  assert.match(marketplaceSource, /['"]skills\.swarmskillshub\.recommend['"]/);
  assert.match(marketplaceSource, /HUB_HOME_TOP_K/);
  assert.match(marketplaceSource, /HUB_MORE_TOP_K/);
  assert.match(marketplaceSource, /plugin_type:\s*pluginType/);
  assert.match(marketplaceSource, /['"]swarmskill['"]/);
  assert.match(marketplaceSource, /['"]skill['"]/);
  assert.match(skillPanelSource, /const HUB_HOME_TOP_K = 6/);
  assert.match(skillPanelSource, /const HUB_MORE_TOP_K = 30/);
  assert.match(marketplaceSource, /category_id: category/);
  assert.match(installationSource, /['"]skills\.online_search\.install['"]/);
  assert.doesNotMatch(marketplaceSource, /\bmarket_url\b|https?:\/\/|\b\d{1,3}(?:\.\d{1,3}){3}\b/);
  assert.doesNotMatch(installationSource, /\bmarket_url\b|https?:\/\/|\b\d{1,3}(?:\.\d{1,3}){3}\b/);
});
