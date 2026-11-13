import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  localStorage: dom.window.localStorage,
  sessionStorage: dom.window.sessionStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
after(() => dom.window.close());
const { createRoot } = await import('react-dom/client');
await i18next.use(initReactI18next).init({
  lng: 'en', showSupportNotice: false, resources: { en: { translation: {} } },
});
const { MarketplaceView } = await import('../node_modules/.cache/skill-marketplace/MarketplaceView.mjs');
const { groupMarketplaceSkills, resolveSkillFileReference } = await import(
  '../node_modules/.cache/skill-marketplace/skillPanelUtils.mjs'
);

const skill = { asset_id: 'ordinary', name: 'Ordinary Skill', plugin_type: 'skill', skill_type: 'skill' };
const legacy = {
  asset_id: 'legacy-pack', name: 'Legacy Pack', plugin_type: 'skill', skill_type: 'skillpack',
  short_desc: '[技能包] 生成站会摘要和行动报告', tags: ['openjiuwen-skillpack-v1'],
};
const native = { ...legacy, asset_id: 'native-pack', name: 'Native Pack', plugin_type: 'skillpack' };
const team = { asset_id: 'team', name: 'Team Skill', plugin_type: 'swarmskill', skill_type: 'swarm_skill' };

test('ordinary Skills and both Pack transport types share Featured Skills without losing their type', () => {
  const groups = groupMarketplaceSkills([legacy, skill, team, native]);
  assert.deepEqual(groups.teamSkills, [team]);
  assert.deepEqual(groups.featuredSkills, [legacy, skill, native]);
  assert.equal(groups.featuredSkills[0], legacy);
  assert.equal(groups.featuredSkills[0].skill_type, 'skillpack');
  assert.equal(groups.featuredSkills[0].short_desc, legacy.short_desc);
  assert.deepEqual(groupMarketplaceSkills([]), { teamSkills: [], featuredSkills: [] });
});

test('resolves relative skill file references from the active Markdown file', () => {
  assert.equal(resolveSkillFileReference('SKILL.md', 'references/guide.md'), 'references/guide.md');
  assert.equal(resolveSkillFileReference('references/current.md', '../scripts/run.py#usage'), 'scripts/run.py');
  assert.equal(resolveSkillFileReference('references/current.md', './next%20step.md?plain=1'), 'references/next step.md');
  assert.equal(resolveSkillFileReference('references/current.md', '..\\assets\\diagram.svg'), 'assets/diagram.svg');
  assert.equal(resolveSkillFileReference('SKILL.md', '#overview'), null);
  assert.equal(resolveSkillFileReference('SKILL.md', 'https://example.com/guide.md'), null);
  assert.equal(resolveSkillFileReference('SKILL.md', '/workspace/guide.md'), null);
  assert.equal(resolveSkillFileReference('SKILL.md', '../outside.md'), null);
});

for (const searchKeyword of ['', 'standup']) {
  test(`${searchKeyword ? 'search' : 'home'} keeps Pack cards in the shared list and passes typed selection to detail`, async () => {
    const items = [skill, legacy, native];
    let selected;
    let more;
    const root = createRoot(document.getElementById('root'));
    try {
      await act(async () => root.render(React.createElement(MarketplaceView, {
        marketplaceSubView: 'list', ...groupMarketplaceSkills(items),
        hubTeamMore: [], hubSkillMore: items, hubSkills: items,
        hubLoading: false, hubMoreLoading: false, searchKeyword, marketplaceCategory: '',
        onSelectHubSkill: item => { selected = item; },
        renderHubSkillAction: () => ({}), onCategoryChange: () => {},
        onOpenMore: kind => { more = kind; }, onBackFromMore: () => {},
      })));
      const cards = [...document.querySelectorAll('[data-testid="skill-panel-hub-card"]')];
      assert.equal(cards.length, 3);
      assert.equal(document.body.textContent.includes(i18next.t('skills.featuredSkillPacks')), false);
      assert.match(cards[1].textContent, /\[技能包\] 生成站会摘要和行动报告/);
      await act(async () => cards[1].click());
      assert.equal(selected, legacy);
      assert.equal(selected.skill_type, 'skillpack');
      if (!searchKeyword) {
        assert.ok(document.body.textContent.includes(i18next.t('skills.featuredSkills')));
        const moreButton = document.querySelector('[data-testid="skill-panel-featured-skills-more-btn"]');
        assert.ok(moreButton);
        await act(async () => moreButton.click());
        assert.equal(more, 'skill');
      }
    } finally {
      await act(async () => root.unmount());
    }
  });
}
