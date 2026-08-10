export const ORCHESTRATOR_TOOL_NAME = "orchestrator";

export function workerToolNames(activeTools: readonly string[]): string[] {
  return activeTools.filter((name) => name !== ORCHESTRATOR_TOOL_NAME);
}
