export const ORCHESTRATOR_TOOL_NAME = "orchestrator";
export const ROOM_TOOL_NAME = "room";

export function workerToolNames(activeTools: readonly string[], roomEnabled = false): string[] {
  return activeTools.filter((name) =>
    name !== ORCHESTRATOR_TOOL_NAME && (roomEnabled || name !== ROOM_TOOL_NAME),
  );
}
