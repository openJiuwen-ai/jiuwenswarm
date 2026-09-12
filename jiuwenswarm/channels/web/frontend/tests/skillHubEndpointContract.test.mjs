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
    'const fetchHubSkills = useCallback',
    'const fetchOnlineSearch = useCallback',
  );
  const installationSource = sourceBetween(
    'const handleInstallHubSkill = useCallback',
    'const fetchSkillVersions = useCallback',
  );

  assert.match(marketplaceSource, /['"]skills\.swarmskillshub\.recommend['"]/);
  assert.match(marketplaceSource, /top_k: 50/);
  assert.match(marketplaceSource, /category_id: category/);
  assert.match(installationSource, /['"]skills\.teamskillshub\.install['"]/);
  assert.doesNotMatch(marketplaceSource, /\bmarket_url\b|https?:\/\/|\b\d{1,3}(?:\.\d{1,3}){3}\b/);
  assert.doesNotMatch(installationSource, /\bmarket_url\b|https?:\/\/|\b\d{1,3}(?:\.\d{1,3}){3}\b/);
});
