import type { ConnectorConnectResponse } from '../../types/connector';

export const connectReaIfNeeded = async (
  connectionState: string | undefined,
  connect: (name: string) => Promise<ConnectorConnectResponse | null>,
): Promise<boolean> => {
  if (connectionState === 'connected') return true;
  try {
    const response = await connect('rea');
    return response?.type === 'connected';
  } catch {
    return false;
  }
};

export const shouldAutoConnectRea = (userAgent: string): boolean =>
  /Macintosh|Mac OS X/i.test(userAgent);
