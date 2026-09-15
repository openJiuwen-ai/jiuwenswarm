import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ensureClipboardImageFilename,
  getClipboardImageFiles,
  IMAGE_INPUT_DISABLED_ALERT_KEY,
  isImageInputDisabled,
  shouldAlertImagePasteDisabled,
} from '../node_modules/.cache/clipboard-image-paste/clipboardImagePaste.js';

test('Agent mode keeps image input enabled while interruptible so attachments can queue', () => {
  assert.equal(
    isImageInputDisabled({
      isListening: false,
      isCompactRunning: false,
      isInterruptible: true,
      isTeamMode: false,
      isAgentMode: true,
    }),
    false,
  );
});

test('non-agent interruptible mode still disables image input', () => {
  assert.equal(
    isImageInputDisabled({
      isListening: false,
      isCompactRunning: false,
      isInterruptible: true,
      isTeamMode: false,
      isAgentMode: false,
    }),
    true,
  );
});

test('team mode allows images while interruptible', () => {
  assert.equal(
    isImageInputDisabled({
      isListening: false,
      isCompactRunning: false,
      isInterruptible: true,
      isTeamMode: true,
      isAgentMode: false,
    }),
    false,
  );
});

test('desktop/browser paste blocked by imageInputDisabled surfaces addFileDisabled alert', () => {
  assert.equal(shouldAlertImagePasteDisabled(true, true), true);
  assert.equal(shouldAlertImagePasteDisabled(true, false), false);
  assert.equal(shouldAlertImagePasteDisabled(false, true), false);
  assert.equal(IMAGE_INPUT_DISABLED_ALERT_KEY, 'chat.addFileDisabled');
});

test('same name/size/MIME but different content are both kept', () => {
  const a = new File([new Uint8Array([1, 2, 3, 4])], 'paste.png', { type: 'image/png' });
  const b = new File([new Uint8Array([9, 8, 7, 6])], 'paste.png', { type: 'image/png' });
  assert.equal(a.size, b.size);
  assert.equal(a.name, b.name);
  assert.equal(a.type, b.type);

  const files = getClipboardImageFiles({
    items: [
      { kind: 'file', getAsFile: () => a },
      { kind: 'file', getAsFile: () => b },
    ],
  });

  assert.equal(files.length, 2);
  assert.notEqual(files[0], files[1]);
});

test('nameless PNG/JPEG screenshots get clipboard-image filenames', () => {
  const png = ensureClipboardImageFilename(new File([new Uint8Array([1])], '', { type: 'image/png' }));
  const jpeg = ensureClipboardImageFilename(new File([new Uint8Array([2])], '', { type: 'image/jpeg' }));

  assert.equal(png.name, 'clipboard-image.png');
  assert.equal(png.type, 'image/png');
  assert.equal(jpeg.name, 'clipboard-image.jpg');
  assert.equal(jpeg.type, 'image/jpeg');

  const fromClipboard = getClipboardImageFiles({
    items: [
      {
        kind: 'file',
        getAsFile: () => new File([new Uint8Array([3])], '', { type: 'image/png' }),
      },
    ],
  });
  assert.equal(fromClipboard.length, 1);
  assert.equal(fromClipboard[0].name, 'clipboard-image.png');
});
