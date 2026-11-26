import assert from 'node:assert/strict';
import test from 'node:test';
import {
  acquireSessionViewportToken,
  deleteSessionViewport,
  getSessionScrollRestore,
  getSessionTimelineViewport,
  releaseSessionViewportToken,
  saveSessionScrollTop as saveSessionScrollTopWithToken,
  saveSessionTimelineViewport as saveSessionTimelineViewportWithToken,
} from '../node_modules/.cache/virtual-timeline/sessionViewportCache.mjs';

function timeline(offset) {
  return {
    measurements: [{ index: 4, key: 'message-4', start: offset, end: offset + 120, size: 120, lane: 0 }],
    offset,
  };
}

function mountedWriter(sessionId) {
  const token = acquireSessionViewportToken(sessionId);
  assert.ok(token);
  return token;
}

function saveSessionScrollTop(sessionId, scrollTop, atBottom) {
  const token = acquireSessionViewportToken(sessionId);
  saveSessionScrollTopWithToken(token, scrollTop, atBottom);
  releaseSessionViewportToken(token);
}

function saveSessionTimelineViewport(sessionId, state) {
  const token = acquireSessionViewportToken(sessionId);
  saveSessionTimelineViewportWithToken(token, state);
  releaseSessionViewportToken(token);
}

test('scroll offset and measured timeline survive independently in the same session', () => {
  const id = 'cache-independent-fields';
  saveSessionScrollTop(id, 512, false);
  saveSessionTimelineViewport(id, timeline(512));
  assert.deepEqual(getSessionScrollRestore(id, 2000, 500), { scrollTop: 512, wasAtBottom: false, hasTimelineAnchor: false });
  assert.deepEqual(getSessionTimelineViewport(id), timeline(512));

  saveSessionScrollTop(id, 768, false);
  assert.deepEqual(getSessionScrollRestore(id, 2000, 500), { scrollTop: 768, wasAtBottom: false, hasTimelineAnchor: false });
  assert.deepEqual(getSessionTimelineViewport(id), timeline(512));
  deleteSessionViewport(id);
});

test('sessions are isolated and deletion removes both viewport layers', () => {
  saveSessionScrollTop('cache-a', 100, false);
  saveSessionTimelineViewport('cache-a', timeline(100));
  saveSessionScrollTop('cache-b', 200, true);
  saveSessionTimelineViewport('cache-b', timeline(200));

  deleteSessionViewport('cache-a');
  assert.equal(getSessionScrollRestore('cache-a', 1000, 400), undefined);
  assert.equal(getSessionTimelineViewport('cache-a'), undefined);
  assert.deepEqual(getSessionScrollRestore('cache-b', 1000, 400), { scrollTop: 600, wasAtBottom: true, hasTimelineAnchor: false });
  assert.deepEqual(getSessionTimelineViewport('cache-b'), timeline(200));
  deleteSessionViewport('cache-b');
});

test('empty session IDs do not create shared viewport entries', () => {
  saveSessionScrollTop('', 100, false);
  saveSessionTimelineViewport('', timeline(100));
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

  assert.deepEqual(getSessionScrollRestore('cache-capacity-0', 2000, 500), { scrollTop: 1, wasAtBottom: false, hasTimelineAnchor: false });
  assert.deepEqual(getSessionTimelineViewport('cache-capacity-0'), timeline(1));
  assert.equal(getSessionScrollRestore('cache-capacity-1', 2000, 500), undefined);
  assert.deepEqual(getSessionScrollRestore('cache-capacity-20', 3000, 500), { scrollTop: 2500, wasAtBottom: true, hasTimelineAnchor: false });

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
    hasTimelineAnchor: false,
  });
  assert.deepEqual(getSessionScrollRestore('cache-reading', 4003, 467), {
    scrollTop: 1336,
    wasAtBottom: false,
    hasTimelineAnchor: false,
  });
  assert.equal(getSessionScrollRestore('cache-missing', 4003, 467), undefined);

  deleteSessionViewport('cache-bottom');
  deleteSessionViewport('cache-reading');
});

test('restore exposes a stable timeline anchor for ChatPanel coordination', () => {
  const id = 'cache-anchor-metadata';
  saveSessionScrollTop(id, 2560, false);
  saveSessionTimelineViewport(id, { ...timeline(2560), anchorKey: 'row-20', anchorOffset: 24 });

  assert.deepEqual(getSessionScrollRestore(id, 4000, 600), {
    scrollTop: 2560,
    wasAtBottom: false,
    hasTimelineAnchor: true,
  });
  deleteSessionViewport(id);
});

test('permanent deletion invalidates both mounted writers without evicting other sessions', () => {
  const deletedId = 'cache-deleted-current';
  const retainedId = 'cache-deleted-retained';
  const scrollWriter = mountedWriter(deletedId);
  const timelineWriter = mountedWriter(deletedId);
  saveSessionScrollTopWithToken(scrollWriter, 2560, false);
  saveSessionTimelineViewportWithToken(timelineWriter, timeline(2560));
  saveSessionScrollTop(retainedId, 900, false);
  saveSessionTimelineViewport(retainedId, timeline(900));

  deleteSessionViewport(deletedId);
  saveSessionScrollTopWithToken(scrollWriter, 2600, false);
  saveSessionTimelineViewportWithToken(timelineWriter, timeline(2600));
  releaseSessionViewportToken(scrollWriter);
  releaseSessionViewportToken(timelineWriter);

  assert.equal(getSessionScrollRestore(deletedId, 4000, 600), undefined);
  assert.equal(getSessionTimelineViewport(deletedId), undefined);
  assert.deepEqual(getSessionScrollRestore(retainedId, 2000, 500), {
    scrollTop: 900,
    wasAtBottom: false,
    hasTimelineAnchor: false,
  });
  assert.deepEqual(getSessionTimelineViewport(retainedId), timeline(900));
  deleteSessionViewport(retainedId);
});

test('normal navigation saves both writer snapshots in either unmount order', () => {
  for (const order of [['scroll', 'timeline'], ['timeline', 'scroll']]) {
    const id = `cache-normal-${order.join('-')}`;
    const writers = {
      scroll: mountedWriter(id),
      timeline: mountedWriter(id),
    };

    for (const kind of order) {
      if (kind === 'scroll') saveSessionScrollTopWithToken(writers.scroll, 512, false);
      else saveSessionTimelineViewportWithToken(writers.timeline, timeline(768));
      releaseSessionViewportToken(writers[kind]);
    }

    assert.deepEqual(getSessionScrollRestore(id, 2000, 500), {
      scrollTop: 512,
      wasAtBottom: false,
      hasTimelineAnchor: false,
    });
    assert.deepEqual(getSessionTimelineViewport(id), timeline(768));

    const remountedWriter = mountedWriter(id);
    saveSessionScrollTopWithToken(remountedWriter, 1024, false);
    releaseSessionViewportToken(remountedWriter);
    assert.deepEqual(getSessionScrollRestore(id, 2000, 500), {
      scrollTop: 1024,
      wasAtBottom: false,
      hasTimelineAnchor: false,
    });
    assert.deepEqual(getSessionTimelineViewport(id), timeline(768));
    deleteSessionViewport(id);
  }
});

test('deleted generations reject both old writers after twenty other deletions in either unmount order', () => {
  for (const order of [['scroll', 'timeline'], ['timeline', 'scroll']]) {
    const id = `cache-deleted-${order.join('-')}`;
    const writers = {
      scroll: mountedWriter(id),
      timeline: mountedWriter(id),
    };
    saveSessionScrollTopWithToken(writers.scroll, 2560, false);
    saveSessionTimelineViewportWithToken(writers.timeline, timeline(2560));

    deleteSessionViewport(id);
    for (let index = 0; index < 20; index += 1) {
      deleteSessionViewport(`${id}-other-${index}`);
    }

    for (const kind of order) {
      if (kind === 'scroll') saveSessionScrollTopWithToken(writers.scroll, 3200, false);
      else saveSessionTimelineViewportWithToken(writers.timeline, timeline(3200));
      releaseSessionViewportToken(writers[kind]);
      assert.equal(getSessionScrollRestore(id, 12800, 600), undefined);
      assert.equal(getSessionTimelineViewport(id), undefined);
    }
  }
});
