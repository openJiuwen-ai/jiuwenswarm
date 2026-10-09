/**
 * The platform's display name, in the interface's language.
 *
 * The gateway sends a `provider_name` alongside every row and connection, but it
 * is a fixed string chosen where the connection is read, not a translated one --
 * so an English interface showed a Chinese platform name. The interface owns what
 * it calls things, so the name is looked up from the provider key here.
 *
 * The gateway's string stays as the fallback: it is what a provider this build
 * does not know yet will be called, which beats printing the bare key.
 */
const PROVIDER_KEY: Record<string, string> = {
  google: 'docs.providerGoogle',
  feishu: 'docs.providerFeishu',
};

export function providerLabel(
  provider: string | undefined,
  providerName: string | undefined,
  t: (k: string, o?: Record<string, unknown>) => string,
): string {
  const key = PROVIDER_KEY[provider || ''];
  if (key) return t(key, { defaultValue: providerName || provider || '' });
  return providerName || provider || '';
}

type ConnLike = { id: string; agent_display?: string; agent_address?: string };

/** The address the connection acts under, in full. Belongs in a tooltip, not a column. */
export function connFullName(c: ConnLike): string {
  return c.agent_address || c.agent_display || c.id;
}

/**
 * A short name per connection, unique within the set it is given.
 *
 * The table's connection column exists to say which identity owns a row, and a
 * service-account address is forty-five characters of which about six carry that
 * meaning. But the obvious shortening -- everything before the `@` -- is wrong
 * here: two service accounts in different projects share the local part
 * (`docs-agent@openjiuwen…` and `docs-agent@openjiuwen-505320…`), and a column
 * that renders both identically fails at its one job.
 *
 * So the name is only as long as it needs to be. Start at the local part; where
 * that collides, add the first label of the domain, which is what actually
 * differs; where even that collides, keep the whole address rather than show two
 * rows as the same identity.
 */
export function connShortNames(conns: ConnLike[]): Record<string, string> {
  // The address, never the display name. A Feishu connection's display name is
  // the app's ("bot"), which names no particular identity; its address is the
  // bot's open id, which does. An address with no `@` has nothing to shorten and
  // is used whole.
  const localOf = (c: ConnLike): string => {
    const full = connFullName(c);
    return full.includes('@') ? full.split('@')[0] || c.id : full;
  };
  const domainOf = (c: ConnLike): string => connFullName(c).split('@')[1]?.split('.')[0] || '';

  const short: Record<string, string> = {};
  const byLocal = new Map<string, ConnLike[]>();
  for (const c of conns) {
    const l = localOf(c);
    byLocal.set(l, [...(byLocal.get(l) || []), c]);
  }
  for (const [local, group] of byLocal) {
    if (group.length === 1) {
      short[group[0].id] = local;
      continue;
    }
    const withDomain = new Map<string, ConnLike[]>();
    for (const c of group) {
      const k = domainOf(c) ? `${local}@${domainOf(c)}` : connFullName(c);
      withDomain.set(k, [...(withDomain.get(k) || []), c]);
    }
    for (const [name, sub] of withDomain) {
      for (const c of sub) short[c.id] = sub.length === 1 ? name : connFullName(c);
    }
  }
  return short;
}
