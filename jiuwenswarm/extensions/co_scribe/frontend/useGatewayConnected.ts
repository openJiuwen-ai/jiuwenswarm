import { useEffect, useState } from 'react';

import { webClient } from '../../../channels/web/frontend/src/services/webClient';

/**
 * Whether the gateway socket is usable, read from the client itself.
 *
 * The host threads this down as a prop to the views it owns, but an
 * application plugin's page is mounted by ``ApplicationPluginOutlet`` with no
 * props at all. The same fact is public on the web client, and ``ready`` is
 * exactly the state the host's own ``isConnected`` is derived from
 * (``useWebSocket``), so the two never disagree.
 */
export function useGatewayConnected(): boolean {
  const [connected, setConnected] = useState(() => webClient.getState() === 'ready');
  useEffect(() => webClient.onStateChange(state => setConnected(state === 'ready')), []);
  return connected;
}
