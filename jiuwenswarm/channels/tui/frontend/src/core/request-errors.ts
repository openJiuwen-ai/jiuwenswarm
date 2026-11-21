const UNCERTAIN_CODES = ["AGENT_SERVER_TIMEOUT", "SERVICE_UNAVAILABLE", "REQUEST_TIMEOUT", "TRANSPORT_DISCONNECTED"];

export function requestErrorCode(error: unknown): string | undefined {
  return error instanceof Error && "code" in error && typeof error.code === "string" ? error.code : undefined;
}

/** Whether `method` may have taken effect although its reply was lost. Older Gateways report this by message only. */
export function isUncertainRequestError(error: unknown, method: string): boolean {
  if (!(error instanceof Error)) return false;
  return (
    UNCERTAIN_CODES.includes(requestErrorCode(error) ?? "") ||
    error.message.startsWith(`request timeout: ${method}`) ||
    error.message.includes("AgentServer request timed out") ||
    error.message === "request failed: AGENT_SERVER_TIMEOUT" ||
    error.message.includes("AgentServer WebSocket connection closed") ||
    error.message.startsWith("agent server request failed: ") ||
    error.message === "disconnected"
  );
}
