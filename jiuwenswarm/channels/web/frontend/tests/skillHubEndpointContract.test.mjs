import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const hubMarketplaceSource = readFileSync(
  new URL('../src/components/SkillPanel/useHubMarketplace.ts', import.meta.url),
  'utf8',
);
const skillPanelSource = readFileSync(new URL('../src/components/SkillPanel/index.tsx', import.meta.url), 'utf8');

function sourceBetween(source, start, end) {
  const startIndex = source.indexOf(start);
  const endIndex = source.indexOf(end, startIndex);
  assert.notEqual(startIndex, -1, `missing source marker: ${start}`);
  assert.notEqual(endIndex, -1, `missing source marker: ${end}`);
  return source.slice(startIndex, endIndex);
}

test('Skill Hub marketplace and installation rely on the server-configured Hub', () => {
  const marketplaceSource = sourceBetween(
    hubMarketplaceSource,
    'const fetchHubRecommendByType = useCallback',
    'const fetchOnlineSearch = useCallback',
  );
  const homeSource = sourceBetween(
    hubMarketplaceSource,
    'const fetchHubHomeSkills = useCallback',
    'const fetchHubMoreSkills = useCallback',
  );
  const installationSource = sourceBetween(
    skillPanelSource,
    'const handleInstallHubSkill = useCallback',
    'const fetchSkillVersions = useCallback',
  );

  assert.match(marketplaceSource, /['"]skills\.swarmskillshub\.recommend['"]/);
  assert.match(marketplaceSource, /HUB_HOME_TOP_K/);
  assert.match(marketplaceSource, /HUB_MORE_TOP_K/);
  assert.match(marketplaceSource, /plugin_type:\s*pluginType/);
  assert.match(marketplaceSource, /['"]swarmskill['"]/);
  assert.match(marketplaceSource, /['"]skill['"]/);
  assert.match(hubMarketplaceSource, /const HUB_HOME_TOP_K = 6/);
  assert.match(hubMarketplaceSource, /const HUB_MORE_TOP_K = 500/);
  assert.match(marketplaceSource, /category_id: category/);
  assert.match(hubMarketplaceSource, /hubHomeLoadedCategoryRef/);
  assert.match(hubMarketplaceSource, /const silent = hubHomeLoadedCategoryRef\.current === category/);
  // 先 prefer_cache；cold miss 且无旧卡时立刻直连 Hub（不空等轮询）
  assert.match(homeSource, /preferCache:\s*true/);
  assert.match(homeSource, /preferCache:\s*false/);
  assert.match(homeSource, /isCatalogMissRefreshing/);
  assert.match(homeSource, /loadOne\('swarmskill'/);
  assert.match(homeSource, /loadOne\('skill'/);
  assert.doesNotMatch(homeSource, /loadOne\('skillpack'|fetchHubRecommendByType\(category,\s*'skillpack'/);
  assert.match(
    sourceBetween(hubMarketplaceSource, 'const pauseHubFetching = useCallback', 'return {'),
    /setHubSkills\(\[\]\)/,
  );
  assert.doesNotMatch(
    sourceBetween(hubMarketplaceSource, 'const pauseHubFetching = useCallback', 'return {'),
    /setHubTeamHome\(\[\]\)/,
  );
  assert.match(installationSource, /['"]skills\.online_search\.install['"]/);
  assert.doesNotMatch(marketplaceSource, /\bmarket_url\b|https?:\/\/|\b\d{1,3}(?:\.\d{1,3}){3}\b/);
  assert.doesNotMatch(installationSource, /\bmarket_url\b|https?:\/\/|\b\d{1,3}(?:\.\d{1,3}){3}\b/);
});
