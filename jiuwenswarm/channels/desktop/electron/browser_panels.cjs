const { createHash } = require('node:crypto');

const SHARED_BROWSER_PARTITION = 'persist:jiuwenswarm-browser';

function panelIdentity(sessionId, memberId = '', label = '') {
  const sid = String(sessionId || '').trim() || 'default';
  const member = String(memberId || '').trim();
  const panelId = member
    ? `member:${createHash('sha256').update(JSON.stringify([sid, member])).digest('hex')}`
    : sid;
  return { panelId, sessionId: sid, memberId: member, label: String(label || member).trim() };
}

function processAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    // Permission errors must not turn a live owner's page into an eviction candidate.
    return error.code !== 'ESRCH';
  }
}

function hasLiveLease(entry, isAlive = processAlive) {
  for (const [id, pid] of entry.leases) {
    if (!isAlive(pid)) entry.leases.delete(id);
  }
  return entry.leases.size > 0;
}

function evictionCandidates(views, preserveId, activeId, isAlive = processAlive) {
  return [...views.entries()]
    .filter(([id, entry]) => id !== preserveId && id !== activeId && !entry.visible
      && !entry.creating && !hasLiveLease(entry, isAlive))
    .sort(([, a], [, b]) => a.lastActive - b.lastActive);
}

function createTargetHandler({ token, ensureView, getView, changed }) {
  return async (req, res) => {
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.setHeader('Cache-Control', 'no-store');
    const reply = (status, body) => {
      res.statusCode = status;
      res.end(JSON.stringify(body));
    };
    // This endpoint is for local MCP clients, never browser-origin requests.
    if (req.headers.origin || req.headers.authorization !== `Bearer ${token}`) {
      reply(403, { error: 'browser target authorization required' });
      return;
    }
    try {
      const url = new URL(req.url || '/', 'http://127.0.0.1');
      const sessionId = decodeURIComponent(url.pathname.replace(/^\/+/, ''));
      if (!sessionId) return reply(400, { error: 'session id required' });
      const identity = panelIdentity(sessionId, url.searchParams.get('member'), url.searchParams.get('label'));
      const leaseId = url.searchParams.get('lease');
      const ownerPid = Number(url.searchParams.get('pid'));
      if (!leaseId || !Number.isSafeInteger(ownerPid) || ownerPid <= 0) {
        return reply(400, { error: 'MCP lease and owner pid required' });
      }
      if (req.method === 'DELETE') {
        const entry = getView(identity.panelId);
        if (entry?.leases.get(leaseId) === ownerPid) {
          entry.leases.delete(leaseId);
          changed();
        }
        return reply(200, { released: true });
      }
      if (req.method !== 'POST') return reply(405, { error: 'POST or DELETE required' });
      const entry = await ensureView(identity, { leaseId, ownerPid });
      if (!entry) return reply(503, { error: 'browser sideview disabled' });
      changed();
      return reply(200, { ...identity, targetId: entry.targetId });
    } catch (error) {
      return reply(500, { error: String(error?.message || error) });
    }
  };
}

module.exports = { SHARED_BROWSER_PARTITION, panelIdentity, hasLiveLease, evictionCandidates, createTargetHandler };
