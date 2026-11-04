export interface RequestBinding {
  type: 'http' | 'websocket';
  source: { key: string; path?: string[] };
  target: { location: 'header'; name: string; prefix?: string }
    | { location: 'message'; path: string[]; prefix?: string };
}

/** Supplied by a trusted host adapter, never by a render tree or UI event. */
export interface ClientDeclarationSnapshot {
  clientProtocolVersion: 1;
  gatewayInstanceId: string;
  connectionEpoch: string;
  packages: {
    packageId: string;
    activationGeneration: string;
    grants: string[];
    requestBindings: RequestBinding[];
  }[];
}

export interface ClientContextChange {
  epoch: number;
  phase: 'changing' | 'ready';
}

export type ClientOperation = {
  clientProtocolVersion: 1;
  operation_id: string;
  invocation_id: string;
  connection_epoch: string;
  package_id: string;
  activation_generation: string;
  /** Unix epoch milliseconds, checked before executing the operation. */
  deadline: number;
} & (
  | { method: 'local_storage.set'; params: { key: string; value: unknown } }
  | { method: 'local_storage.delete'; params: { key: string } }
  | { method: 'page.reload'; params: Record<string, never> }
);
