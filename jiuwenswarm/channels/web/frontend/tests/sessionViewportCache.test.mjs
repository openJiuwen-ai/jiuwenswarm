import assert from 'node:assert/strict';
import test from 'node:test';
import {
  deleteSessionViewport,
  getSessionScrollRestore,
  getSessionScrollTop,
  getSessionTimelineViewport,
  saveSessionScrollTop,
  saveSessionTimelineViewport,
} from '../node_modules/.cache/virtual-timeline/sessionViewportCache.mjs';

function timeline(offset) {
  return {
    measurements: [{ index: 4, key: 'message-4', start: offset, end: offset + 120, size: 120, lane: 0 }],
    offset,
  };
}

test('scroll offset and measured timeline survive independently in the same session', () => {
  const id = 'cache-independent-fields';
  saveSessionScrollTop(id, 512, false);
  saveSessionTimelineViewport(id, timeline(512));
  assert.equal(getSessionScrollTop(id), 512);
  assert.deepEqual(getSessionTimelineViewport(id), timeline(512));

  saveSessionScrollTop(id, 768, false);
  assert.equal(getSessionScrollTop(id), 768);
  assert.deepEqual(getSessionScrollRestore(id, 2000, 500), { scrollTop: 768, wasAtBottom: false });
  assert.deepEqual(getSessionTimelineViewport(id), timeline(512));
  deleteSessionViewport(id);
});

test('sessions are isolated and deletion removes both viewport layers', () => {
  saveSessionScrollTop('cache-a', 100, false);
  saveSessionTimelineViewport('cache-a', timeline(100));
  saveSessionScrollTop('cache-b', 200, true);
  saveSessionTimelineViewport('cache-b', timeline(200));

  deleteSessionViewport('cache-a');
  assert.equal(getSessionScrollTop('cache-a'), undefined);
  assert.equal(getSessionScrollRestore('cache-a', 1000, 400), undefined);
  assert.equal(getSessionTimelineViewport('cache-a'), undefined);
  assert.equal(getSessionScrollTop('cache-b'), 200);
  assert.deepEqual(getSessionTimelineViewport('cache-b'), timeline(200));
  deleteSessionViewport('cache-b');
});

test('empty session IDs do not create shared viewport entries', () => {
  saveSessionScrollTop('', 100, false);
  saveSessionTimelineViewport('', timeline(100));
  assert.equal(getSessionScrollTop(''), undefined);
  assert.equal(getSessionScrollRestore('', 1000, 400), undefined);
  assert.equal(getSessionTimelineViewport(''), undefined);
});

test('the bounded cache evicts the oldest session and refreshes recently written entries', () => {
  for (let index = 0; index < 20; index += 1) {
    saveSessionScrollTop(`cache-capacity-${index}`, index * 100, false);
  }
  saveSessionScrollTop('cache-capacity-0', 1, false);
  saveSessionTimelineViewport('cache-capacity-0', timeline(1));
  saveSessionScrollTop('cache-capacity-20', 2000, true);

  assert.equal(getSessionScrollTop('cache-capacity-0'), 1);
  assert.deepEqual(getSessionTimelineViewport('cache-capacity-0'), timeline(1));
  assert.equal(getSessionScrollTop('cache-capacity-1'), undefined);
  assert.equal(getSessionScrollTop('cache-capacity-20'), 2000);

  for (let index = 0; index <= 20; index += 1) {
    deleteSessionViewport(`cache-capacity-${index}`);
  }
});

test('bottom restoration follows content growth without moving an upward reading anchor', () => {
  saveSessionScrollTop('cache-bottom', 2536, true);
  saveSessionScrollTop('cache-reading', 1336, false);

  assert.deepEqual(getSessionScrollRestore('cache-bottom', 4003, 467), {
    scrollTop: 3536,
    wasAtBottom: true,
  });
  assert.deepEqual(getSessionScrollRestore('cache-reading', 4003, 467), {
    scrollTop: 1336,
    wasAtBottom: false,
  });
  assert.equal(getSessionScrollRestore('cache-missing', 4003, 467), undefined);

  deleteSessionViewport('cache-bottom');
  deleteSessionViewport('cache-reading');
});
