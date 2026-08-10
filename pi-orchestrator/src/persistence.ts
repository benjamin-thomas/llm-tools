import type { ThinkingLevel } from "@earendil-works/pi-agent-core";
import type { ScopedModelSpec, WorkerRecord } from "./types.js";

export const ORCHESTRATION_STATE_TYPE = "pi-orchestrator/state";
export const PERSISTED_STATE_VERSION = 1 as const;

export interface PersistedWorker {
  id: string;
  slot: number;
  name: string;
  cwd: string;
  model: ScopedModelSpec;
  thinkingLevel: ThinkingLevel;
  sessionId?: string;
  sessionFile?: string;
  createdAt: number;
  readCursor: string | null;
  unreadCount: number;
}

export type PersistedOrchestrationState =
  | { version: typeof PERSISTED_STATE_VERSION; active: false }
  | {
      version: typeof PERSISTED_STATE_VERSION;
      active: true;
      scopedModels: ScopedModelSpec[];
      workers: PersistedWorker[];
      previousFocusedId?: string;
    };

export function createPersistedState(
  scopedModels: readonly ScopedModelSpec[],
  workers: readonly WorkerRecord[],
  previousFocusedId?: string | null,
): PersistedOrchestrationState {
  const persistedWorkers = workers
    .filter((worker) => worker.activity !== "stopped")
    .map((worker) => ({
      id: worker.id,
      slot: worker.slot,
      name: worker.name,
      cwd: worker.cwd,
      model: { ...worker.model },
      thinkingLevel: worker.thinkingLevel,
      ...(worker.sessionId ? { sessionId: worker.sessionId } : {}),
      ...(worker.sessionFile ? { sessionFile: worker.sessionFile } : {}),
      createdAt: worker.createdAt,
      readCursor: worker.readCursor,
      unreadCount: worker.unreadCount,
    }));
  const validPreviousId = previousFocusedId === "__orchestrator__"
    || persistedWorkers.some((worker) => worker.id === previousFocusedId)
    ? previousFocusedId
    : null;
  return {
    version: PERSISTED_STATE_VERSION,
    active: true,
    scopedModels: scopedModels.map((model) => ({ ...model })),
    ...(validPreviousId ? { previousFocusedId: validPreviousId } : {}),
    workers: persistedWorkers,
  };
}

export function inactivePersistedState(): PersistedOrchestrationState {
  return { version: PERSISTED_STATE_VERSION, active: false };
}

interface EntryLike {
  type?: unknown;
  customType?: unknown;
  data?: unknown;
}

export function findPersistedState(entries: readonly EntryLike[]): PersistedOrchestrationState | null {
  for (let index = entries.length - 1; index >= 0; index--) {
    const entry = entries[index]!;
    if (entry.type !== "custom" || entry.customType !== ORCHESTRATION_STATE_TYPE) continue;
    return isPersistedState(entry.data) ? entry.data : null;
  }
  return null;
}

function isPersistedState(value: unknown): value is PersistedOrchestrationState {
  if (!isRecord(value) || value.version !== PERSISTED_STATE_VERSION || typeof value.active !== "boolean") {
    return false;
  }
  if (!value.active) return true;
  if (!Array.isArray(value.scopedModels) || !value.scopedModels.every(isModelSpec)) return false;
  if (!Array.isArray(value.workers) || !value.workers.every(isPersistedWorker)) return false;
  const slots = new Set(value.workers.map((worker) => worker.slot));
  const ids = new Set(value.workers.map((worker) => worker.id));
  const previousIsValid = value.previousFocusedId === undefined
    || value.previousFocusedId === "__orchestrator__"
    || (typeof value.previousFocusedId === "string" && ids.has(value.previousFocusedId));
  return slots.size === value.workers.length
    && ids.size === value.workers.length
    && previousIsValid;
}

function isPersistedWorker(value: unknown): value is PersistedWorker {
  return isRecord(value)
    && typeof value.id === "string"
    && typeof value.slot === "number"
    && Number.isInteger(value.slot)
    && value.slot >= 5
    && value.slot <= 12
    && typeof value.name === "string"
    && typeof value.cwd === "string"
    && isModelSpec(value.model)
    && isThinkingLevel(value.thinkingLevel)
    && (value.sessionId === undefined || typeof value.sessionId === "string")
    && (value.sessionFile === undefined || typeof value.sessionFile === "string")
    && typeof value.createdAt === "number"
    && (value.readCursor === null || typeof value.readCursor === "string")
    && typeof value.unreadCount === "number"
    && Number.isInteger(value.unreadCount)
    && value.unreadCount >= 0;
}

function isModelSpec(value: unknown): value is ScopedModelSpec {
  return isRecord(value)
    && typeof value.provider === "string"
    && typeof value.id === "string"
    && (value.thinkingLevel === undefined || isThinkingLevel(value.thinkingLevel));
}

function isThinkingLevel(value: unknown): value is ThinkingLevel {
  return value === "off"
    || value === "minimal"
    || value === "low"
    || value === "medium"
    || value === "high"
    || value === "xhigh"
    || value === "max";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
