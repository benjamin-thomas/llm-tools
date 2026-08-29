import type { ThinkingLevel } from "@earendil-works/pi-agent-core";
import { createRoomState, type RoomState } from "./room.js";
import type { OrchestrationMode, ScopedModelSpec, WorkerRecord } from "./types.js";

export const ORCHESTRATION_STATE_TYPE = "pi-orchestrator/state";
export const PERSISTED_STATE_VERSION = 2 as const;

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
      mode: OrchestrationMode;
      scopedModels: ScopedModelSpec[];
      workers: PersistedWorker[];
      room?: RoomState;
      previousFocusedId?: string;
    };

export function createPersistedState(
  scopedModels: readonly ScopedModelSpec[],
  workers: readonly WorkerRecord[],
  previousFocusedId?: string | null,
  mode: OrchestrationMode = "silo",
  room?: RoomState,
): PersistedOrchestrationState {
  const persistedWorkers = workers
    .filter((worker) => worker.activity !== "stopped" && worker.sessionFile !== undefined)
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
    mode,
    scopedModels: scopedModels.map((model) => ({ ...model })),
    ...(mode === "room" ? { room: structuredClone(room ?? createRoomState()) } : {}),
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

export function findPersistedState(
  entries: readonly EntryLike[],
  onInvalid?: () => void,
): PersistedOrchestrationState | null {
  for (let index = entries.length - 1; index >= 0; index--) {
    const entry = entries[index]!;
    if (entry.type !== "custom" || entry.customType !== ORCHESTRATION_STATE_TYPE) continue;
    if (isPersistedState(entry.data)) return entry.data;
    if (isLegacyPersistedState(entry.data)) {
      if (!entry.data.active) return inactivePersistedState();
      return {
        version: PERSISTED_STATE_VERSION,
        active: true,
        mode: "silo",
        scopedModels: entry.data.scopedModels,
        workers: entry.data.workers,
        ...(entry.data.previousFocusedId ? { previousFocusedId: entry.data.previousFocusedId } : {}),
      };
    }
    onInvalid?.();
  }
  return null;
}

function isPersistedState(value: unknown): value is PersistedOrchestrationState {
  if (!isRecord(value) || value.version !== PERSISTED_STATE_VERSION || typeof value.active !== "boolean") {
    return false;
  }
  if (!value.active) return true;
  if (value.mode !== "silo" && value.mode !== "room") return false;
  if (!isActiveStateBase(value)) return false;
  if (value.mode === "room" && !isRoomState(value.room)) return false;
  return value.mode !== "silo" || value.room === undefined;
}

function isLegacyPersistedState(value: unknown): value is {
  version: 1;
  active: boolean;
  scopedModels: ScopedModelSpec[];
  workers: PersistedWorker[];
  previousFocusedId?: string;
} {
  if (!isRecord(value) || value.version !== 1 || typeof value.active !== "boolean") return false;
  if (!value.active) return true;
  return isActiveStateBase(value);
}

function isActiveStateBase(value: Record<string, unknown>): value is Record<string, unknown> & {
  scopedModels: ScopedModelSpec[];
  workers: PersistedWorker[];
  previousFocusedId?: string;
} {
  if (!Array.isArray(value.scopedModels) || !value.scopedModels.every(isModelSpec)) return false;
  if (!Array.isArray(value.workers) || !value.workers.every(isPersistedWorker)) return false;
  const slots = new Set(value.workers.map((worker) => worker.slot));
  const ids = new Set(value.workers.map((worker) => worker.id));
  const names = new Set(value.workers.map((worker) => worker.name.toLowerCase()));
  const previousIsValid = value.previousFocusedId === undefined
    || value.previousFocusedId === "__orchestrator__"
    || (typeof value.previousFocusedId === "string" && ids.has(value.previousFocusedId));
  return slots.size === value.workers.length
    && ids.size === value.workers.length
    && names.size === value.workers.length
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
    && value.name.length > 0
    && typeof value.cwd === "string"
    && value.cwd.length > 0
    && isModelSpec(value.model)
    && isThinkingLevel(value.thinkingLevel)
    && (value.sessionId === undefined || typeof value.sessionId === "string")
    && (value.sessionFile === undefined || typeof value.sessionFile === "string")
    && typeof value.createdAt === "number"
    && Number.isFinite(value.createdAt)
    && (value.readCursor === null || typeof value.readCursor === "string")
    && typeof value.unreadCount === "number"
    && Number.isInteger(value.unreadCount)
    && value.unreadCount >= 0;
}

function isRoomState(value: unknown): value is RoomState {
  if (!isRecord(value)
    || !Number.isInteger(value.nextSequence)
    || Number(value.nextSequence) < 1
    || !Array.isArray(value.messages)
    || !value.messages.every(isRoomMessage)
    || !Array.isArray(value.obligations)
    || !value.obligations.every(isRoomObligation)
    || !isRecord(value.cursors)
    || !Object.values(value.cursors).every((cursor) => Number.isInteger(cursor) && Number(cursor) >= 0)
    || (value.openBroadcastId !== null && typeof value.openBroadcastId !== "string")
    || !Number.isInteger(value.moderationEvery)
    || Number(value.moderationEvery) < 1
    || Number(value.moderationEvery) > 100
    || !Number.isInteger(value.messagesSinceModeration)
    || Number(value.messagesSinceModeration) < 0
    || typeof value.moderationRequired !== "boolean"
    || typeof value.concluded !== "boolean") return false;

  const messages = value.messages as RoomState["messages"];
  const obligations = value.obligations as RoomState["obligations"];
  const messageIds = new Set(messages.map((message) => message.id));
  const sequences = new Set(messages.map((message) => message.sequence));
  const maxSequence = messages.reduce((maximum, message) => Math.max(maximum, message.sequence), 0);
  if (
    messageIds.size !== messages.length
    || sequences.size !== messages.length
    || messages.some((message, index) => message.sequence !== index + 1)
    || Number(value.nextSequence) !== maxSequence + 1
    || messages.some((message) => message.replyTo !== undefined && !messageIds.has(message.replyTo))
    || obligations.some((obligation) =>
      !messageIds.has(obligation.messageId)
      || (obligation.responseMessageId !== undefined && !messageIds.has(obligation.responseMessageId)),
    )
  ) return false;
  if (value.openBroadcastId !== null) {
    const broadcast = messages.find((message) => message.id === value.openBroadcastId);
    if (!broadcast?.broadcast) return false;
  }
  const obligationKeys = new Set(obligations.map((obligation) =>
    `${obligation.messageId}\0${obligation.workerId}`,
  ));
  return obligationKeys.size === obligations.length;
}

function isRoomMessage(value: unknown): boolean {
  return isRecord(value)
    && typeof value.id === "string"
    && Number.isInteger(value.sequence)
    && Number(value.sequence) >= 1
    && typeof value.timestamp === "number"
    && isRoomAddress(value.sender)
    && Array.isArray(value.recipients)
    && value.recipients.every(isRoomAddress)
    && typeof value.text === "string"
    && typeof value.broadcast === "boolean"
    && typeof value.humanRequest === "boolean"
    && (value.resolved === undefined || typeof value.resolved === "boolean")
    && (value.replyTo === undefined || typeof value.replyTo === "string");
}

function isRoomAddress(value: unknown): boolean {
  return isRecord(value) && typeof value.id === "string" && typeof value.name === "string";
}

function isRoomObligation(value: unknown): boolean {
  return isRecord(value)
    && typeof value.messageId === "string"
    && typeof value.workerId === "string"
    && (value.status === "pending" || value.status === "delivered" || value.status === "responded" || value.status === "failed")
    && (value.responseMessageId === undefined || typeof value.responseMessageId === "string")
    && (value.error === undefined || typeof value.error === "string");
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
