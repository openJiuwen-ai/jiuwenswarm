export interface ResourceReading {
  cpuPercent: number | null;
  memoryBytes: number | null;
  shared?: boolean;
}

export interface SystemResources {
  cpuPercent: number | null;
  memoryPercent: number | null;
  sampledAt: number;
}

export interface MarketResources {
  apps: (ResourceReading & { appId: string })[];
  system?: SystemResources;
  highUsage?: boolean;
  sampledAt: number;
}

