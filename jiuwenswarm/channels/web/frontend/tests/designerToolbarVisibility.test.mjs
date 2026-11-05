import assert from 'node:assert/strict';
import test from 'node:test';

import { designerToolbarRevealShift } from '../node_modules/.cache/designer-toolbar-visibility/designerToolbarVisibility.js';
import { isDesignerModerationError } from '../node_modules/.cache/designer-toolbar-visibility/designerRunView.js';

test('a toolbar already inside the canvas needs no pan', () => {
  assert.equal(designerToolbarRevealShift({ top: 200, bottom: 400 }, { top: 0, bottom: 600 }), 0);
});

test('a toolbar below the canvas edge pans up just enough to show it', () => {
  assert.equal(designerToolbarRevealShift({ top: 500, bottom: 700 }, { top: 0, bottom: 600 }), 116);
});

test('a toolbar taller than the canvas keeps its top edge in view', () => {
  assert.equal(designerToolbarRevealShift({ top: 100, bottom: 900 }, { top: 0, bottom: 600 }), 84);
});

test('provider moderation rejections are recognised', () => {
  assert.equal(
    isDesignerModerationError(
      'clip generation failed: Video gen failed for shot 9; image→mp4 fallback is disabled: '
        + '[ERROR]: video job 453956174270886 ended with status failed: 1026 content blocked by moderation',
    ),
    true,
  );
  assert.equal(isDesignerModerationError('[ERROR]: DashScope video job failed: DataInspectionFailed'), true);
  assert.equal(isDesignerModerationError('OutputVideoSensitiveContentDetected'), true);
});

test('other failures keep their original message', () => {
  assert.equal(isDesignerModerationError('upstream node failed: n_clip_9 — node not executed'), false);
  assert.equal(isDesignerModerationError('Video generation is switched off'), false);
  assert.equal(isDesignerModerationError(''), false);
  assert.equal(isDesignerModerationError(null), false);
});
