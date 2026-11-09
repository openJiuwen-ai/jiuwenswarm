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
const { FilePreviewContent } = await import('../node_modules/.cache/skill-marketplace/FilePreviewContent.mjs');
const { MarkdownPane } = await import('../node_modules/.cache/skill-marketplace/MarkdownPane.mjs');
const { groupMarketplaceSkills, resolveSkillFileLinkPath } = await import(
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

test('SKILL.md file links resolve inside the skill package', () => {
  assert.equal(resolveSkillFileLinkPath('SKILL.md', 'references/api.md'), 'references/api.md');
  assert.equal(resolveSkillFileLinkPath('SKILL.md', './references/api.md#usage'), 'references/api.md');
  assert.equal(resolveSkillFileLinkPath('docs/SKILL.md', '../templates/report.md'), 'templates/report.md');
  assert.equal(
    resolveSkillFileLinkPath('docs/SKILL.md', './assets/My%20Guide.md?download=1'),
    'docs/assets/My Guide.md',
  );
  assert.equal(resolveSkillFileLinkPath('SKILL.md', '/references/guide.md'), 'references/guide.md');
  assert.equal(resolveSkillFileLinkPath('docs/SKILL.md', '../../outside.md'), null);
  assert.equal(resolveSkillFileLinkPath('SKILL.md', 'https://example.com/guide.md'), null);
  assert.equal(resolveSkillFileLinkPath('SKILL.md', '#section'), null);
});

test('decodes encoded paths before validating the skill package boundary', () => {
  assert.equal(resolveSkillFileLinkPath('SKILL.md', '%2e%2e/outside.md'), null);
  assert.equal(resolveSkillFileLinkPath('docs/SKILL.md', '..%2f..%2foutside.md'), null);
  assert.equal(resolveSkillFileLinkPath('SKILL.md', '../outside.md'), null);
});

test('preview Markdown forwards referenced file clicks instead of navigating the page', async () => {
  let clickedHref = null;
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () =>
      root.render(
        React.createElement(FilePreviewContent, {
          selected: true,
          selectedPreviewable: true,
          file: { path: 'SKILL.md', content: '[API guide](references/api.md#usage)' },
          status: 'success',
          labels: {
            selectPrompt: '',
            notPreviewable: '',
            loading: '',
            readError: '',
            copy: '',
            copied: '',
            copyFailed: '',
            download: '',
            downloadFailed: '',
            noPreview: '',
            binaryDownload: '',
          },
          onLinkClick: (href) => {
            clickedHref = href;
            return true;
          },
        }),
      ),
    );
    const link = document.querySelector('[data-testid="file-preview-content-markdown"] a');
    assert.ok(link);
    const event = new dom.window.MouseEvent('click', { bubbles: true, cancelable: true });
    await act(async () => link.dispatchEvent(event));
    assert.equal(clickedHref, 'references/api.md#usage');
    assert.equal(event.defaultPrevented, true);
    assert.equal(link.hasAttribute('target'), false);
  } finally {
    await act(async () => root.unmount());
  }
});

test('content detail Markdown forwards referenced file clicks instead of navigating the page', async () => {
  let clickedHref = null;
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () =>
      root.render(
        React.createElement(MarkdownPane, {
          content: '[API guide](references/api.md#usage)',
          onLinkClick: (href) => {
            clickedHref = href;
            return true;
          },
        }),
      ),
    );
    const link = document.querySelector('a[href="references/api.md#usage"]');
    assert.ok(link);
    const event = new dom.window.MouseEvent('click', { bubbles: true, cancelable: true });
    await act(async () => link.dispatchEvent(event));
    assert.equal(clickedHref, 'references/api.md#usage');
    assert.equal(event.defaultPrevented, true);
    assert.equal(link.hasAttribute('target'), false);
  } finally {
    await act(async () => root.unmount());
  }
});

test('MarkdownPane preserves default link behavior when onLinkClick returns false', async () => {
  let clickedHref = null;
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () =>
      root.render(
        React.createElement(MarkdownPane, {
          content: '[Deferred target](./guide.md)',
          onLinkClick: (href) => {
            clickedHref = href;
            return false;
          },
        }),
      ),
    );
    const link = document.querySelector('a[href="./guide.md"]');
    assert.ok(link);
    link.removeAttribute('href');
    const event = new dom.window.MouseEvent('click', { bubbles: true, cancelable: true });
    await act(async () => link.dispatchEvent(event));
    assert.equal(clickedHref, './guide.md');
    assert.equal(event.defaultPrevented, false);
  } finally {
    await act(async () => root.unmount());
  }
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
