import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const helpTips = readFileSync(new URL('../src/components/ui/HelpTips/HelpTips.tsx', import.meta.url), 'utf8');
const adaptive = readFileSync(new URL('../src/hooks/useAdaptiveTooltip.tsx', import.meta.url), 'utf8');

test('HelpTips uses adaptive tooltips instead of the native title attribute', () => {
  assert.match(helpTips, /useAdaptiveTooltip/);
  assert.match(helpTips, /data-tooltip=\{content\}/);
  assert.doesNotMatch(helpTips, /\btitle=\{content\}/);
});

test('adaptive tooltips portal into the nearest dialog when present', () => {
  assert.match(adaptive, /closest\('dialog'\)\s*\?\?\s*document\.body/);
  assert.match(adaptive, /dialogRect/);
});
