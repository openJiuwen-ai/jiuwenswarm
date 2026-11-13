import assert from 'node:assert/strict';
import test from 'node:test';
import { teamFollowUpLiveReady } from '../node_modules/.cache/team-follow-up-live/sessionLive.mjs';

const idle = { sessionId: '', ready: false };
const existingSession = 'web_session';

test('personal edition can send a follow-up without a session live subscription', () => {
  assert.equal(teamFollowUpLiveReady(false, existingSession, idle), true);
  assert.equal(teamFollowUpLiveReady(false, 'new', idle), true);
});

test('enterprise follow-up waits until the live subscription matches the session', () => {
  assert.equal(teamFollowUpLiveReady(true, existingSession, idle), false);
  assert.equal(
    teamFollowUpLiveReady(true, existingSession, { sessionId: 'other', ready: true }),
    false,
  );
  assert.equal(
    teamFollowUpLiveReady(true, existingSession, { sessionId: existingSession, ready: true }),
    true,
  );
  assert.equal(teamFollowUpLiveReady(true, 'new', idle), true);
});
