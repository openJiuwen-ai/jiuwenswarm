import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const css = await readFile(new URL('../src/features/jiuwenmate/jiuwenmate.css', import.meta.url), 'utf8');

const composerButton = '.jiuwenmate-composer__toolbar button:hover:not(:disabled)';
const sendButtonHover = '.jiuwenmate-composer__toolbar .jiuwenmate-composer__send:hover:not(:disabled)';

/** Declarations of the rule whose selector is exactly `selector`. */
const ruleBody = (selector) => {
  const start = css.indexOf(`${selector} {`);
  assert.notEqual(start, -1, `missing rule: ${selector}`);
  const end = css.indexOf('}', start);
  return css.slice(start + selector.length + 2, end);
};

/** Specificity as [classes + pseudo-classes, elements]; `:not(x)` contributes its argument. */
const specificity = (selector) => {
  const notArguments = [...selector.matchAll(/:not\(([^)]*)\)/g)].map((match) => match[1]);
  const rest = selector.replace(/:not\([^)]*\)/g, '');
  const count = (text, pattern) => (text.match(pattern) ?? []).length;
  const [classes, elements] = notArguments.reduce(
    (total, argument) => {
      const [argumentClasses, argumentElements] = specificity(argument);
      return [total[0] + argumentClasses, total[1] + argumentElements];
    },
    [count(rest, /\.[\w-]+/g) + count(rest, /:(?!:)[\w-]+/g), count(rest, /(?:^|[\s>+~])[a-zA-Z][\w-]*/g)],
  );
  return [classes, elements];
};

const compareSpecificity = ([leftClasses, leftElements], [rightClasses, rightElements]) =>
  leftClasses - rightClasses || leftElements - rightElements;

const relativeLuminance = (hex) => {
  const [r, g, b] = [1, 3, 5]
    .map((offset) => parseInt(hex.slice(offset, offset + 2), 16) / 255)
    .map((channel) => (channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

test('composer send and stop buttons keep a dark background on hover', () => {
  const token = css.match(/--jm-dark-hover:\s*(#[0-9a-f]{6});/);
  assert.ok(token, 'jiuwenmate shell should define --jm-dark-hover');
  const body = ruleBody(sendButtonHover);
  assert.match(body, /background:\s*var\(--jm-dark-hover\)/);
  assert.ok(
    compareSpecificity(specificity(sendButtonHover), specificity(composerButton)) > 0,
    `${sendButtonHover} must outrank ${composerButton}`,
  );
});

test('hover background keeps the white send icon readable', () => {
  const token = css.match(/--jm-dark-hover:\s*(#[0-9a-f]{6});/);
  assert.ok(token);
  const contrast = 1.05 / (relativeLuminance(token[1]) + 0.05);
  assert.ok(contrast >= 4.5, `contrast against #fff is ${contrast.toFixed(2)}:1`);
});

test('other composer toolbar buttons keep the muted hover background', () => {
  assert.match(ruleBody(composerButton), /background:\s*var\(--jm-muted\)/);
});
