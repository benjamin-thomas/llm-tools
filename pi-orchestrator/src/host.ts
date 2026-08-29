import type { ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { ThinkingLevel } from "@earendil-works/pi-agent-core";
import { clampThinkingLevel, type Model } from "@earendil-works/pi-ai";
import { SelectList, type Component } from "@earendil-works/pi-tui";
import {
  createActivationQueue,
  requestSerializedActivation,
  runSerializedActivation,
  type ActivationQueue,
} from "./activation.js";
import { assertSupportedPiVersion } from "./compatibility.js";
import {
  captureDispatchReceipt,
  countUnreadResponses,
  dispatchToWorker,
  findPromptResponseByMarker,
  readWorkerMessagesWithRecovery,
  waitForWorker,
  type Delivery,
  type DispatchSession,
  type ReadSession,
  type WaitSession,
} from "./communication.js";
import {
  assertWorkerCanReset,
  createWorkerRecord,
  defaultModelForSlot,
  modelKey,
  nextWorkerSlot,
  prepareWorkerForReset,
  renameWorker,
  resolveSpawnModels,
  configuredModelOrder,
} from "./registry.js";
import {
  createPersistedState,
  inactivePersistedState,
  ORCHESTRATION_STATE_TYPE,
  type PersistedOrchestrationState,
} from "./persistence.js";
import {
  configureRoomModeration,
  createRoomState,
  failRoomObligation,
  markRoomDelivered,
  moderateRoom,
  pendingHumanRequests,
  postRoomMessage,
  recordRoomResponse,
  resolveHumanRequest,
  roomMessageSettled,
  roomObligationsForMessage,
  unreadRoomMessages,
  type RoomMessage,
  type RoomParticipant,
  type RoomState,
} from "./room.js";
import { createWorkerHandle, type WorkerHandle } from "./runtime.js";
import {
  FIRST_WORKER_KEY,
  ORCHESTRATOR_ID,
  SHARED_STATE_VERSION,
  type CoordinatorRecord,
  type OrchestrationMode,
  type OrchestratorSnapshot,
  type ScopedModelSpec,
  type SpawnRequest,
  type WorkerRecord,
} from "./types.js";
import {
  OrchestratorWidget,
  preferredSwitcherIndex,
  switchTargetForKey,
} from "./ui.js";

interface RuntimeWorker {
  record: WorkerRecord;
  handle: WorkerHandle | null;
}

interface ParentTui {
  start(): void;
  stop(): void;
  requestRender(force?: boolean): void;
}

export interface SharedHost {
  version: typeof SHARED_STATE_VERSION;
  active: boolean;
  mode: OrchestrationMode;
  room: RoomState | null;
  roomPumps: Set<string>;
  tearingDownWorkerIds: Set<string>;
  focusedId: string;
  previousFocusedId?: string | null;
  coordinator: CoordinatorRecord | null;
  scopedModels: ScopedModelSpec[];
  workers: RuntimeWorker[];
  subscribers: Set<() => void>;
  activation: ActivationQueue;
  parentTui: ParentTui | null;
  parentDone: (() => void) | null;
  parentHandoffActive: boolean;
  persist: (() => void) | null;
  lastPersistedState: string | null;
}

const SHARED_KEY = Symbol.for("pi-orchestrator.host.v4");

function newHost(): SharedHost {
  return {
    version: SHARED_STATE_VERSION,
    active: false,
    mode: "silo",
    room: null,
    roomPumps: new Set(),
    tearingDownWorkerIds: new Set(),
    focusedId: ORCHESTRATOR_ID,
    previousFocusedId: null,
    coordinator: null,
    scopedModels: [],
    workers: [],
    subscribers: new Set(),
    activation: createActivationQueue(),
    parentTui: null,
    parentDone: null,
    parentHandoffActive: false,
    persist: null,
    lastPersistedState: null,
  };
}

export function getHost(): SharedHost {
  const storage = globalThis as unknown as Record<PropertyKey, unknown>;
  const existing = storage[SHARED_KEY] as SharedHost | undefined;
  if (existing?.version === SHARED_STATE_VERSION) return existing;
  const host = newHost();
  storage[SHARED_KEY] = host;
  return host;
}

function notify(host: SharedHost): void {
  try {
    host.persist?.();
  } catch {
    // Persistence must not interrupt worker lifecycle or terminal handoff.
  }
  for (const subscriber of [...host.subscribers]) {
    try {
      subscriber();
    } catch {
      // A disposed TUI may leave a callback behind briefly during handoff.
    }
  }
}

export function subscribe(host: SharedHost, callback: () => void): () => void {
  host.subscribers.add(callback);
  return () => host.subscribers.delete(callback);
}

function scopedSpec(model: Model<any>, thinkingLevel?: ThinkingLevel): ScopedModelSpec {
  return thinkingLevel === undefined
    ? { provider: model.provider, id: model.id }
    : { provider: model.provider, id: model.id, thinkingLevel };
}

export function reconcileThinkingLevelForModel(model: Model<any>, level: ThinkingLevel): ThinkingLevel {
  return clampThinkingLevel(model, level);
}

export type PersistenceWriter = (customType: string, data: unknown) => void;

function configurePersistence(host: SharedHost, writeEntry?: PersistenceWriter): void {
  host.lastPersistedState = null;
  if (!writeEntry) {
    host.persist = null;
    return;
  }
  host.persist = () => {
    if (!host.active) return;
    const state = createPersistedState(
      host.scopedModels,
      host.workers.map(({ record }) => record),
      host.previousFocusedId,
      host.mode,
      host.room ?? undefined,
    );
    const serialized = JSON.stringify(state);
    if (serialized === host.lastPersistedState) return;
    writeEntry(ORCHESTRATION_STATE_TYPE, state);
    host.lastPersistedState = serialized;
  };
}

export function attachPersistence(host: SharedHost, writeEntry: PersistenceWriter): void {
  configurePersistence(host, writeEntry);
}

function clearRuntimeState(host: SharedHost): void {
  for (const worker of host.workers) worker.handle?.dispose().catch(() => {});
  host.active = false;
  host.mode = "silo";
  host.room = null;
  host.roomPumps.clear();
  host.tearingDownWorkerIds.clear();
  host.focusedId = ORCHESTRATOR_ID;
  host.previousFocusedId = null;
  host.coordinator = null;
  host.scopedModels = [];
  host.workers = [];
  host.parentTui = null;
  host.parentDone = null;
  host.parentHandoffActive = false;
  host.activation = createActivationQueue();
  host.persist = null;
  host.lastPersistedState = null;
}

export function activateOrchestrator(
  ctx: ExtensionContext,
  writeEntry?: PersistenceWriter,
  mode: OrchestrationMode = "silo",
): SharedHost {
  assertSupportedPiVersion();
  const host = getHost();
  if (ctx.scopedModels.length === 0) {
    throw new Error("No scoped models are configured. Configure /scoped-models first.");
  }

  const now = Date.now();
  const sessionFile = ctx.sessionManager.getSessionFile();
  const model = ctx.model ? scopedSpec(ctx.model) : undefined;
  for (const worker of host.workers) worker.handle?.dispose().catch(() => {});
  host.workers = [];
  host.active = true;
  host.mode = mode;
  host.room = mode === "room" ? createRoomState() : null;
  host.roomPumps.clear();
  host.tearingDownWorkerIds.clear();
  host.focusedId = ORCHESTRATOR_ID;
  host.previousFocusedId = null;
  host.scopedModels = configuredModelOrder(
    ctx.scopedModels.map(({ model: candidate, thinkingLevel }) =>
      scopedSpec(candidate, thinkingLevel),
    ),
  );
  host.coordinator = {
    id: ORCHESTRATOR_ID,
    name: "orchestrator",
    cwd: ctx.cwd,
    ...(model ? { model } : {}),
    thinkingLevel: ctx.thinkingLevel ?? "off",
    activity: ctx.isIdle() ? "idle" : "working",
    sessionId: ctx.sessionManager.getSessionId(),
    ...(sessionFile ? { sessionFile } : {}),
    lastActivityAt: now,
  };
  configurePersistence(host, writeEntry);
  notify(host);
  return host;
}

export async function restoreOrchestrator(
  ctx: ExtensionContext,
  state: PersistedOrchestrationState,
  writeEntry?: PersistenceWriter,
): Promise<SharedHost> {
  assertSupportedPiVersion();
  const host = getHost();
  if (host.active) throw new Error("Cannot restore orchestration while one is already active.");
  if (!state.active) return host;

  host.active = true;
  host.mode = state.mode;
  host.room = state.mode === "room" ? structuredClone(state.room ?? createRoomState()) : null;
  host.roomPumps.clear();
  host.tearingDownWorkerIds.clear();
  host.focusedId = ORCHESTRATOR_ID;
  host.previousFocusedId = state.previousFocusedId ?? null;
  host.scopedModels = configuredModelOrder(
    ctx.scopedModels.map(({ model, thinkingLevel }) =>
      scopedSpec(model, thinkingLevel),
    ),
  );
  const now = Date.now();
  const sessionFile = ctx.sessionManager.getSessionFile();
  const coordinatorModel = ctx.model ? scopedSpec(ctx.model) : undefined;
  host.coordinator = {
    id: ORCHESTRATOR_ID,
    name: "orchestrator",
    cwd: ctx.cwd,
    ...(coordinatorModel ? { model: coordinatorModel } : {}),
    thinkingLevel: ctx.thinkingLevel ?? "off",
    activity: ctx.isIdle() ? "idle" : "working",
    sessionId: ctx.sessionManager.getSessionId(),
    ...(sessionFile ? { sessionFile } : {}),
    lastActivityAt: now,
  };
  host.workers = state.workers.map((saved) => ({
    record: {
      id: saved.id,
      slot: saved.slot,
      name: saved.name,
      cwd: ctx.cwd,
      model: { ...saved.model },
      thinkingLevel: saved.thinkingLevel,
      activity: "creating",
      ...(saved.sessionId ? { sessionId: saved.sessionId } : {}),
      ...(saved.sessionFile ? { sessionFile: saved.sessionFile } : {}),
      createdAt: saved.createdAt,
      lastActivityAt: now,
      readCursor: saved.readCursor,
      unreadCount: saved.unreadCount,
    },
    handle: null,
  }));

  for (const worker of host.workers) {
    try {
      if (!worker.record.sessionFile) throw new Error("Saved worker session file is missing.");
      worker.handle = await createWorkerHandle(
        worker.record,
        () => host.scopedModels,
        {
          isFocused: () => host.focusedId === worker.record.id,
          onState: () => notify(host),
          onError: (error) => {
            if (host.tearingDownWorkerIds.has(worker.record.id) || worker.record.activity === "stopped") return;
            worker.record.activity = "error";
            worker.record.error = error.message;
            worker.record.lastActivityAt = Date.now();
            notify(host);
          },
        },
        { resumeExistingSession: true, roomEnabled: host.mode === "room" },
      );
      worker.record.activity = "idle";
      worker.record.lastActivityAt = Date.now();
    } catch (value) {
      const error = value instanceof Error ? value : new Error(String(value));
      worker.record.activity = "error";
      worker.record.error = error.message;
    }
  }

  configurePersistence(host, writeEntry);
  if (host.room) {
    const pendingMessageIds = new Set<string>();
    for (const obligation of host.room.obligations) {
      if (obligation.status !== "pending" && obligation.status !== "delivered") continue;
      const worker = host.workers.find(({ record }) => record.id === obligation.workerId);
      if (!worker) {
        obligation.status = "failed";
        obligation.error = "Worker is no longer active.";
        continue;
      }
      const recovered = worker.handle
        ? findPromptResponseByMarker(
            worker.handle.runtime.session as unknown as ReadSession,
            `[pi-orchestrator room message ${obligation.messageId}]`,
          )
        : null;
      if (recovered?.response && worker) {
        recordRoomResponse(
          host.room,
          obligation.messageId,
          worker.record,
          recovered.response.text,
        );
        continue;
      }
      obligation.status = "pending";
      pendingMessageIds.add(obligation.messageId);
    }
    if (host.room.openBroadcastId && roomMessageSettled(host.room, host.room.openBroadcastId)) {
      host.room.openBroadcastId = null;
      host.room.moderationRequired = true;
    }
    for (const messageId of pendingMessageIds) scheduleRoomMessage(host, messageId);
  }
  notify(host);
  return host;
}

export function assertCoordinator(currentSessionId: string, coordinatorSessionId: string): void {
  if (currentSessionId !== coordinatorSessionId) {
    throw new Error("Orchestration controls are only available to the orchestrator session.");
  }
}

export function assertCoordinatorContext(host: SharedHost, ctx: ExtensionContext): void {
  if (!host.active || !host.coordinator) throw new Error("Run /orchestrate first.");
  assertCoordinator(ctx.sessionManager.getSessionId(), host.coordinator.sessionId);
}

export function ownerIdForContext(host: SharedHost, ctx: ExtensionContext): string | null {
  const sessionId = ctx.sessionManager.getSessionId();
  const sessionFile = ctx.sessionManager.getSessionFile();
  if (host.coordinator?.sessionId === sessionId) return ORCHESTRATOR_ID;
  const worker = host.workers.find(({ record }) =>
    record.sessionId === sessionId || (sessionFile !== undefined && record.sessionFile === sessionFile),
  );
  if (worker) {
    return worker.record.activity === "stopped" || host.tearingDownWorkerIds.has(worker.record.id)
      ? null
      : worker.record.id;
  }

  // A focused worker can replace its native session before the new extension starts.
  const focusedWorker = host.workers.find(({ record }) =>
    record.id === host.focusedId
    && record.activity !== "stopped"
    && !host.tearingDownWorkerIds.has(record.id),
  );
  if (focusedWorker) {
    focusedWorker.record.sessionId = sessionId;
    if (sessionFile !== undefined) focusedWorker.record.sessionFile = sessionFile;
    return focusedWorker.record.id;
  }
  return null;
}

export async function spawnWorkers(
  host: SharedHost,
  ctx: ExtensionContext,
  request: SpawnRequest,
): Promise<WorkerRecord[]> {
  assertCoordinatorContext(host, ctx);
  const models = resolveSpawnModels(host.scopedModels, request);
  if (host.workers.length + models.length > 8) {
    throw new Error("The first slice supports at most eight live workers.");
  }

  const created: WorkerRecord[] = [];
  const usesExplicitModels = Boolean(request.models?.length);
  for (const requestedModel of models) {
    const currentRecords = host.workers.map((worker) => worker.record);
    const slot = nextWorkerSlot(currentRecords);
    const model = usesExplicitModels
      ? requestedModel
      : defaultModelForSlot(host.scopedModels, slot);
    const record = createWorkerRecord(currentRecords, model, ctx.cwd);
    const runtimeWorker: RuntimeWorker = { record, handle: null };
    host.workers.push(runtimeWorker);
    notify(host);
    try {
      runtimeWorker.handle = await createWorkerHandle(
        record,
        () => host.scopedModels,
        {
          isFocused: () => host.focusedId === record.id,
          onState: () => notify(host),
          onError: (error) => {
            if (host.tearingDownWorkerIds.has(record.id) || record.activity === "stopped") return;
            record.activity = "error";
            record.error = error.message;
            record.lastActivityAt = Date.now();
            notify(host);
          },
        },
        { roomEnabled: host.mode === "room" },
      );
      record.activity = "idle";
      record.lastActivityAt = Date.now();
      if (!host.active || !host.workers.includes(runtimeWorker)) {
        runtimeWorker.handle?.dispose().catch(() => {});
        runtimeWorker.handle = null;
        record.activity = "error";
        record.error = "Orchestration was stopped during spawn.";
        continue;
      }
      created.push(record);
      notify(host);
    } catch (value) {
      const error = value instanceof Error ? value : new Error(String(value));
      record.activity = "error";
      record.error = error.message;
      notify(host);
      throw error;
    }
  }
  return created;
}

export function renameLiveWorker(host: SharedHost, workerId: string, name: string): WorkerRecord {
  const records = host.workers.map((worker) => worker.record);
  const record = renameWorker(records, workerId, name);
  const runtimeWorker = host.workers.find((worker) => worker.record.id === workerId);
  runtimeWorker?.handle?.runtime.session.setSessionName(record.name);
  notify(host);
  return record;
}

export function syncWorkerNameFromContext(
  host: SharedHost,
  ctx: ExtensionContext,
  name: string | undefined,
): void {
  if (!name) return;
  const ownerId = ownerIdForContext(host, ctx);
  if (!ownerId || ownerId === ORCHESTRATOR_ID) return;
  renameWorker(host.workers.map((worker) => worker.record), ownerId, name);
  notify(host);
}

function isRuntimeWorkerAvailable(
  host: SharedHost,
  worker: RuntimeWorker | undefined,
): worker is RuntimeWorker & { handle: WorkerHandle } {
  return Boolean(
    worker?.handle
    && (worker.record.activity === "idle" || worker.record.activity === "working")
    && !host.tearingDownWorkerIds.has(worker.record.id),
  );
}

function requireLiveWorker(host: SharedHost, workerId: string): RuntimeWorker & { handle: WorkerHandle } {
  const worker = host.workers.find(({ record }) => record.id === workerId);
  if (!worker) throw new Error(`Unknown worker: ${workerId}`);
  if (!isRuntimeWorkerAvailable(host, worker)) {
    throw new Error(`Worker is unavailable: ${workerId}`);
  }
  return worker;
}

function refreshUnread(worker: RuntimeWorker & { handle: WorkerHandle }): void {
  try {
    worker.record.unreadCount = countUnreadResponses(
      worker.handle.runtime.session as unknown as ReadSession,
      worker.record.readCursor,
    );
  } catch {
    // A native /new replaces the branch, invalidating old entry cursors.
    worker.record.readCursor = null;
    worker.record.unreadCount = countUnreadResponses(
      worker.handle.runtime.session as unknown as ReadSession,
      null,
    );
  }
}

export async function sendToWorker(
  host: SharedHost,
  ctx: ExtensionContext,
  workerId: string,
  message: string,
  delivery: Delivery = "followUp",
): Promise<{
  dispatchId: string;
  after: string | null;
  delivery: "immediate" | Delivery;
  accepted: true;
}> {
  assertCoordinatorContext(host, ctx);
  const worker = requireLiveWorker(host, workerId);
  const receipt = captureDispatchReceipt(worker.handle.runtime.session);
  worker.record.activity = "working";
  worker.record.lastActivityAt = Date.now();
  notify(host);
  try {
    const acknowledgement = await dispatchToWorker(
      worker.handle.runtime.session as unknown as DispatchSession,
      message,
      delivery,
      {
        onBackgroundError: (error) => {
          if (host.tearingDownWorkerIds.has(workerId) || worker.record.activity === "stopped") return;
          worker.record.error = error.message;
          notify(host);
        },
        onSettled: () => {
          if (host.tearingDownWorkerIds.has(workerId) || worker.record.activity === "stopped") return;
          worker.record.activity = "idle";
          worker.record.lastActivityAt = Date.now();
          refreshUnread(worker);
          notify(host);
        },
      },
    );
    return { ...receipt, ...acknowledgement };
  } catch (error) {
    if (!host.tearingDownWorkerIds.has(workerId)) {
      worker.record.activity = "idle";
      worker.record.lastActivityAt = Date.now();
      notify(host);
    }
    throw error;
  }
}

export async function waitForLiveWorker(
  host: SharedHost,
  ctx: ExtensionContext,
  workerId: string,
  signal?: AbortSignal,
): Promise<void> {
  assertCoordinatorContext(host, ctx);
  const worker = requireLiveWorker(host, workerId);
  await waitForWorker(worker.handle.runtime.session as unknown as WaitSession, signal);
}

export function readLiveWorker(
  host: SharedHost,
  ctx: ExtensionContext,
  workerId: string,
  options: { after?: string; limit?: number },
) {
  assertCoordinatorContext(host, ctx);
  const worker = requireLiveWorker(host, workerId);
  const after = options.after !== undefined ? options.after : worker.record.readCursor;
  const result = readWorkerMessagesWithRecovery(
    worker.handle.runtime.session as unknown as ReadSession,
    after,
    options.limit,
  );
  if (options.after === undefined) {
    worker.record.readCursor = result.cursor;
    refreshUnread(worker);
    notify(host);
  }
  return { ...result, unreadRemaining: worker.record.unreadCount };
}

export function unreadInbox(host: SharedHost, ctx: ExtensionContext) {
  assertCoordinatorContext(host, ctx);
  return host.workers
    .filter(({ record }) => record.unreadCount > 0)
    .map(({ record }) => ({
      workerId: record.id,
      key: String(record.slot - FIRST_WORKER_KEY + 1),
      name: record.name,
      model: `${record.model.provider}/${record.model.id}`,
      unread: record.unreadCount,
      activity: record.activity,
    }));
}

function requireRoom(host: SharedHost): RoomState {
  if (!host.active) throw new Error("Run /orchestrate first.");
  if (host.mode !== "room" || !host.room) throw new Error("This orchestration is using silo mode.");
  return host.room;
}

function roomParticipants(host: SharedHost): RoomParticipant[] {
  return host.workers.map(({ record }) => ({
    id: record.id,
    name: record.name,
    activity: record.activity,
  }));
}

export function roomDeliveryPrompt(room: RoomState, worker: WorkerRecord, message: RoomMessage): {
  text: string;
  cursor: number;
} {
  const priorCursor = room.cursors[worker.id] ?? 0;
  const candidates = room.messages.filter((candidate) => candidate.sequence > priorCursor);
  const unread: RoomMessage[] = [];
  const contextBudget = Math.max(0, 50_000 - message.text.length);
  let contextCharacters = 0;
  let contextMessages = 0;
  let cursor = priorCursor;
  let gap = false;
  for (const candidate of candidates) {
    if (candidate.id === message.id) {
      unread.push(candidate);
      if (!gap) cursor = candidate.sequence;
      continue;
    }
    if (contextMessages >= 49 || contextCharacters + candidate.text.length > contextBudget) {
      gap = true;
      continue;
    }
    unread.push(candidate);
    contextMessages++;
    contextCharacters += candidate.text.length;
    if (!gap) cursor = candidate.sequence;
  }
  if (!unread.some((candidate) => candidate.id === message.id)) unread.push(message);
  unread.sort((left, right) => left.sequence - right.sequence);
  const transcript = unread.map((candidate) => {
    const targets = candidate.broadcast
      ? "#all"
      : candidate.recipients.length > 0
        ? candidate.recipients.map((recipient) => `#${recipient.name}`).join(" ")
        : "#room";
    return `[${candidate.sequence}] ${candidate.sender.name} → ${targets}\n${candidate.text}`;
  }).join("\n\n");
  return {
    cursor,
    text: [
      `[pi-orchestrator room message ${message.id}]`,
      transcript,
      `You were called as #${worker.name} and must respond. Your final assistant response will be published to the shared room automatically.`,
      "Use room post for visible updates or #human requests. Set expectReply only when a direct response is essential; peer response calls are rejected while #all is open.",
    ].filter(Boolean).join("\n\n"),
  };
}

export async function pumpRoomWorker(host: SharedHost, workerId: string): Promise<void> {
  if (host.roomPumps.has(workerId)) return;
  host.roomPumps.add(workerId);
  let currentMessageId: string | undefined;
  try {
    while (host.active && host.mode === "room" && host.room) {
      if (host.room.moderationRequired) break;
      const obligation = host.room.obligations.find((candidate) =>
        candidate.workerId === workerId && candidate.status === "pending",
      );
      if (!obligation) break;
      currentMessageId = obligation.messageId;
      const worker = requireLiveWorker(host, workerId);
      await waitForWorker(worker.handle.runtime.session as unknown as WaitSession);

      const room = host.room;
      if (
        !host.active
        || host.mode !== "room"
        || !room
        || room.moderationRequired
        || obligation.status !== "pending"
      ) {
        currentMessageId = undefined;
        break;
      }
      const message = room.messages.find((candidate) => candidate.id === obligation.messageId);
      if (!message) throw new Error(`Missing room message: ${obligation.messageId}`);

      const packet = roomDeliveryPrompt(room, worker.record, message);
      worker.record.activity = "working";
      worker.record.lastActivityAt = Date.now();
      notify(host);
      await dispatchToWorker(
        worker.handle.runtime.session as unknown as DispatchSession,
        packet.text,
        "followUp",
        {
          onBackgroundError: (error) => {
            if (host.tearingDownWorkerIds.has(workerId)) return;
            worker.record.error = error.message;
            notify(host);
          },
        },
      );
      markRoomDelivered(room, message.id, workerId);
      room.cursors[workerId] = Math.max(room.cursors[workerId] ?? 0, packet.cursor);
      notify(host);

      await waitForWorker(worker.handle.runtime.session as unknown as WaitSession);
      const recovered = findPromptResponseByMarker(
        worker.handle.runtime.session as unknown as ReadSession,
        `[pi-orchestrator room message ${message.id}]`,
      );
      if (!recovered?.response) {
        throw new Error(`${worker.record.name} completed without a room response.`);
      }
      recordRoomResponse(room, message.id, worker.record, recovered.response.text);
      if (!host.tearingDownWorkerIds.has(workerId)) {
        worker.record.activity = "idle";
        worker.record.lastActivityAt = Date.now();
        refreshUnread(worker);
      }
      currentMessageId = undefined;
      notify(host);
    }
  } catch (value) {
    const error = value instanceof Error ? value : new Error(String(value));
    const room = host.room;
    if (room && currentMessageId) {
      try {
        failRoomObligation(room, currentMessageId, workerId, error.message);
      } catch {
        // The obligation may have settled while the runtime was shutting down.
      }
    }
    const worker = host.workers.find(({ record }) => record.id === workerId);
    if (worker && !host.tearingDownWorkerIds.has(workerId)) {
      worker.record.activity = worker.record.activity === "stopped" ? "stopped" : "idle";
      worker.record.error = error.message;
      worker.record.lastActivityAt = Date.now();
    }
    notify(host);
  } finally {
    host.roomPumps.delete(workerId);
    const shouldResume = host.active
      && host.mode === "room"
      && host.room
      && !host.room.moderationRequired
      && host.room.obligations.some((obligation) =>
        obligation.workerId === workerId && obligation.status === "pending",
      );
    if (shouldResume) queueMicrotask(() => void pumpRoomWorker(host, workerId));
  }
}

function scheduleRoomMessage(host: SharedHost, messageId: string): void {
  if (!host.room) return;
  const workerIds = roomObligationsForMessage(host.room, messageId)
    .filter((obligation) => obligation.status === "pending")
    .map((obligation) => obligation.workerId);
  for (const workerId of new Set(workerIds)) void pumpRoomWorker(host, workerId);
}

export function postToRoom(
  host: SharedHost,
  ctx: ExtensionContext,
  to: readonly string[],
  text: string,
  replyTo?: string,
  expectReply?: boolean,
): RoomMessage {
  const room = requireRoom(host);
  const ownerId = ownerIdForContext(host, ctx);
  if (!ownerId) throw new Error("The current session is not a room participant.");
  const worker = host.workers.find(({ record }) => record.id === ownerId)?.record;
  const sender = ownerId === ORCHESTRATOR_ID
    ? { id: ORCHESTRATOR_ID, name: "orchestrator", kind: "orchestrator" as const }
    : { id: ownerId, name: worker?.name ?? ownerId, kind: "worker" as const };
  const message = postRoomMessage(room, {
    sender,
    to,
    text,
    workers: roomParticipants(host),
    ...(expectReply !== undefined ? { expectReply } : {}),
    ...(replyTo ? { replyTo } : {}),
  });
  notify(host);
  scheduleRoomMessage(host, message.id);
  return message;
}

export function readLiveRoom(
  host: SharedHost,
  ctx: ExtensionContext,
  options: { after?: number; limit?: number } = {},
) {
  const room = requireRoom(host);
  const ownerId = ownerIdForContext(host, ctx);
  if (!ownerId) throw new Error("The current session is not a room participant.");
  const result = unreadRoomMessages(room, ownerId, options);
  notify(host);
  return result;
}

export function liveRoomStatus(host: SharedHost) {
  const room = requireRoom(host);
  return {
    mode: host.mode,
    messages: room.messages.length,
    openBroadcastId: room.openBroadcastId,
    moderationEvery: room.moderationEvery,
    messagesSinceModeration: room.messagesSinceModeration,
    moderationRequired: room.moderationRequired,
    concluded: room.concluded,
    pendingHumanRequests: pendingHumanRequests(room),
    obligations: room.obligations.filter((obligation) =>
      obligation.status === "pending" || obligation.status === "delivered" || obligation.status === "failed",
    ),
  };
}

export interface RoomWaitResult {
  messageIds: string[];
  settled: boolean;
  reason: "settled" | "moderation_required" | "human_request";
}

export async function waitForRoomMessage(
  host: SharedHost,
  ctx: ExtensionContext,
  messageId?: string,
  signal?: AbortSignal,
): Promise<RoomWaitResult> {
  assertCoordinatorContext(host, ctx);
  signal?.throwIfAborted();
  const room = requireRoom(host);
  if (messageId && roomObligationsForMessage(room, messageId).length === 0) {
    throw new Error(`Room message has no worker response obligations: ${messageId}`);
  }
  const messageIds = messageId
    ? [messageId]
    : room.openBroadcastId
      ? [room.openBroadcastId]
      : [...new Set(room.obligations
          .filter((obligation) =>
            obligation.status === "pending" || obligation.status === "delivered",
          )
          .map((obligation) => obligation.messageId))];
  const result = (state: RoomState): RoomWaitResult | null => {
    if (messageIds.length > 0 && messageIds.every((id) => roomMessageSettled(state, id))) {
      return { messageIds, settled: true, reason: "settled" };
    }
    if (pendingHumanRequests(state).length > 0) {
      return { messageIds, settled: false, reason: "human_request" };
    }
    if (state.moderationRequired) {
      return { messageIds, settled: false, reason: "moderation_required" };
    }
    if (messageIds.length === 0) {
      return { messageIds, settled: true, reason: "settled" };
    }
    return null;
  };

  const immediate = result(room);
  if (immediate) return immediate;
  return new Promise<RoomWaitResult>((resolve, reject) => {
    let unsubscribe = () => {};
    const cleanup = () => {
      unsubscribe();
      signal?.removeEventListener("abort", onAbort);
    };
    const onAbort = () => {
      cleanup();
      reject(signal?.reason instanceof Error ? signal.reason : new Error("Room wait aborted."));
    };
    unsubscribe = subscribe(host, () => {
      if (!host.active || host.mode !== "room" || !host.room) {
        cleanup();
        reject(new Error("Room orchestration ended while waiting for responses."));
        return;
      }
      const completed = result(host.room);
      if (!completed) return;
      cleanup();
      resolve(completed);
    });
    signal?.addEventListener("abort", onAbort, { once: true });
    if (signal?.aborted) onAbort();
  });
}

export function configureLiveRoom(
  host: SharedHost,
  ctx: ExtensionContext,
  moderationEvery: number,
): void {
  assertCoordinatorContext(host, ctx);
  configureRoomModeration(requireRoom(host), moderationEvery);
  notify(host);
}

export function moderateLiveRoom(
  host: SharedHost,
  ctx: ExtensionContext,
  decision: "continue" | "conclude",
): void {
  assertCoordinatorContext(host, ctx);
  const room = requireRoom(host);
  moderateRoom(room, decision);
  if (decision === "continue") {
    for (const messageId of new Set(room.obligations
      .filter((obligation) => obligation.status === "pending")
      .map((obligation) => obligation.messageId))) {
      scheduleRoomMessage(host, messageId);
    }
  }
  notify(host);
}

export function resolveLiveHumanRequest(
  host: SharedHost,
  ctx: ExtensionContext,
  messageId: string,
): boolean {
  assertCoordinatorContext(host, ctx);
  const resolved = resolveHumanRequest(requireRoom(host), messageId);
  notify(host);
  return resolved;
}

export async function resetWorker(
  host: SharedHost,
  ctx: ExtensionContext,
  workerId: string,
): Promise<WorkerRecord> {
  assertCoordinatorContext(host, ctx);
  const worker: RuntimeWorker = requireLiveWorker(host, workerId);
  const previousHandle = worker.handle!;
  assertWorkerCanReset(previousHandle.runtime.session.isIdle);
  if (host.roomPumps.has(workerId) || host.room?.obligations.some((obligation) =>
    obligation.workerId === workerId
    && (obligation.status === "pending" || obligation.status === "delivered"),
  )) {
    throw new Error("Reset requires the worker to have no outstanding room response.");
  }

  host.tearingDownWorkerIds.add(workerId);
  worker.record.activity = "creating";
  notify(host);

  return runSerializedActivation(host.activation, async () => {
    try {
      if (host.focusedId === workerId) await doActivate(host, ORCHESTRATOR_ID);
      await previousHandle.dispose();
      worker.handle = null;
      prepareWorkerForReset(worker.record);
      if (host.room) delete host.room.cursors[workerId];
      notify(host);

      worker.handle = await createWorkerHandle(
        worker.record,
        () => host.scopedModels,
        {
          isFocused: () => host.focusedId === worker.record.id,
          onState: () => notify(host),
          onError: (error) => {
            if (host.tearingDownWorkerIds.has(workerId)) return;
            worker.record.activity = "error";
            worker.record.error = error.message;
            worker.record.lastActivityAt = Date.now();
            notify(host);
          },
        },
        { roomEnabled: host.mode === "room" },
      );
      worker.record.activity = "idle";
      worker.record.lastActivityAt = Date.now();
      delete worker.record.error;
      return worker.record;
    } catch (value) {
      const error = value instanceof Error ? value : new Error(String(value));
      worker.handle = null;
      worker.record.activity = "error";
      worker.record.error = error.message;
      worker.record.lastActivityAt = Date.now();
      throw error;
    } finally {
      host.tearingDownWorkerIds.delete(workerId);
      notify(host);
    }
  });
}

function resumeCoordinatorTui(host: SharedHost): void {
  host.focusedId = ORCHESTRATOR_ID;
  host.parentTui?.start();
  host.parentTui?.requestRender(true);
  const done = host.parentDone;
  host.parentTui = null;
  host.parentDone = null;
  host.parentHandoffActive = false;
  done?.();
}

export async function stopWorker(host: SharedHost, workerId: string): Promise<void> {
  const worker = host.workers.find(({ record }) => record.id === workerId);
  if (!worker) throw new Error(`Unknown worker: ${workerId}`);
  if (host.tearingDownWorkerIds.has(workerId)) {
    await runSerializedActivation(host.activation, async () => {
      if (!host.workers.includes(worker)) return;
      worker.record.activity = "stopped";
      notify(host);
      try {
        if (host.focusedId === workerId) await doActivate(host, ORCHESTRATOR_ID);
        if (host.room) {
          for (const obligation of host.room.obligations) {
            if (obligation.workerId !== workerId || obligation.status === "responded" || obligation.status === "failed") continue;
            failRoomObligation(host.room, obligation.messageId, workerId, "Worker stopped before responding.");
          }
        }
        await worker.handle?.dispose();
      } finally {
        const index = host.workers.indexOf(worker);
        if (index >= 0) host.workers.splice(index, 1);
        if (host.focusedId === workerId) resumeCoordinatorTui(host);
        if (host.previousFocusedId === workerId) host.previousFocusedId = null;
        host.tearingDownWorkerIds.delete(workerId);
        notify(host);
      }
    });
    return;
  }

  host.tearingDownWorkerIds.add(workerId);
  worker.record.activity = "stopped";
  if (host.room) {
    for (const obligation of host.room.obligations) {
      if (obligation.workerId !== workerId || obligation.status === "responded" || obligation.status === "failed") continue;
      failRoomObligation(host.room, obligation.messageId, workerId, "Worker stopped before responding.");
    }
  }
  notify(host);

  await runSerializedActivation(host.activation, async () => {
    try {
      if (host.focusedId === workerId) await doActivate(host, ORCHESTRATOR_ID);
      await worker.handle?.dispose();
    } finally {
      const index = host.workers.indexOf(worker);
      if (index >= 0) host.workers.splice(index, 1);
      if (host.focusedId === workerId) resumeCoordinatorTui(host);
      if (host.previousFocusedId === workerId) host.previousFocusedId = null;
      host.tearingDownWorkerIds.delete(workerId);
      notify(host);
    }
  });
}

async function doActivate(host: SharedHost, targetId: string): Promise<void> {
  const target = targetId === ORCHESTRATOR_ID
    ? undefined
    : host.workers.find(({ record }) => record.id === targetId);
  if (targetId !== ORCHESTRATOR_ID && !isRuntimeWorkerAvailable(host, target)) {
    throw new Error(`Worker is unavailable: ${targetId}`);
  }
  if (targetId === host.focusedId) return;

  const priorFocusedId = host.focusedId;
  const current = host.workers.find(({ record }) => record.id === priorFocusedId);
  current?.handle?.suspend();

  host.previousFocusedId = priorFocusedId;
  if (targetId === ORCHESTRATOR_ID) {
    resumeCoordinatorTui(host);
    notify(host);
    return;
  }
  if (!target?.handle) throw new Error(`Worker is unavailable: ${targetId}`);

  host.focusedId = targetId;
  if (target.handle.started) target.handle.resume();
  else target.handle.start();
  notify(host);
}

export async function activate(host: SharedHost, targetId: string): Promise<void> {
  await requestSerializedActivation(host.activation, targetId, (target) => doActivate(host, target));
}

async function enterFromOrchestrator(
  host: SharedHost,
  ctx: ExtensionContext,
  targetId: string,
): Promise<void> {
  if (host.parentHandoffActive) {
    await activate(host, targetId);
    return;
  }
  await ctx.ui.custom<void>((tui, _theme, _keybindings, done) => {
    host.parentTui = tui;
    host.parentDone = done;
    host.parentHandoffActive = true;
    tui.stop();
    process.stdout.write("\x1b[<999u\x1b[>4;0m");
    void activate(host, targetId).catch((value: unknown) => {
      tui.start();
      tui.requestRender(true);
      host.parentTui = null;
      host.parentDone = null;
      host.parentHandoffActive = false;
      ctx.ui.notify(value instanceof Error ? value.message : String(value), "error");
      done();
    });
    const empty: Component = { render: () => [], invalidate: () => {} };
    return empty;
  });
}

export async function activateFromContext(
  host: SharedHost,
  ctx: ExtensionContext,
  targetId: string,
): Promise<void> {
  if (!host.active) throw new Error("Run /orchestrate first.");
  const ownerId = ownerIdForContext(host, ctx);
  if (ownerId === ORCHESTRATOR_ID && targetId !== ORCHESTRATOR_ID) {
    await enterFromOrchestrator(host, ctx, targetId);
    return;
  }
  await activate(host, targetId);
}

export function updateActivity(host: SharedHost, ctx: ExtensionContext, activity: "idle" | "working"): void {
  if (!host.active) return;
  const ownerId = ownerIdForContext(host, ctx);
  if (ownerId === ORCHESTRATOR_ID && host.coordinator) {
    host.coordinator.activity = activity;
    host.coordinator.lastActivityAt = Date.now();
  } else {
    const worker = host.workers.find(({ record }) => record.id === ownerId)?.record;
    if (
      worker
      && worker.activity !== "creating"
      && worker.activity !== "stopped"
      && !host.tearingDownWorkerIds.has(worker.id)
    ) {
      worker.activity = activity;
      worker.lastActivityAt = Date.now();
      if (activity === "idle") {
        const runtimeWorker = host.workers.find(({ record }) => record.id === worker.id);
        if (runtimeWorker?.handle) refreshUnread(runtimeWorker as RuntimeWorker & { handle: WorkerHandle });
      }
    }
  }
  notify(host);
}

export function updateModel(host: SharedHost, ctx: ExtensionContext, model: Model<any>): void {
  const ownerId = ownerIdForContext(host, ctx);
  const spec = scopedSpec(model);
  if (ownerId === ORCHESTRATOR_ID && host.coordinator) {
    host.coordinator.model = spec;
    host.coordinator.thinkingLevel = reconcileThinkingLevelForModel(model, host.coordinator.thinkingLevel);
  } else {
    const worker = host.workers.find(({ record }) => record.id === ownerId)?.record;
    if (worker) {
      worker.model = spec;
      worker.thinkingLevel = reconcileThinkingLevelForModel(model, worker.thinkingLevel);
    }
  }
  notify(host);
}

export function updateThinking(host: SharedHost, ctx: ExtensionContext, level: ThinkingLevel): void {
  const ownerId = ownerIdForContext(host, ctx);
  if (ownerId === ORCHESTRATOR_ID && host.coordinator) host.coordinator.thinkingLevel = level;
  else {
    const worker = host.workers.find(({ record }) => record.id === ownerId)?.record;
    if (worker) worker.thinkingLevel = level;
  }
  notify(host);
}

export function snapshot(host: SharedHost): OrchestratorSnapshot {
  return {
    active: host.active,
    mode: host.mode,
    focusedId: host.focusedId,
    ...(host.room
      ? {
          room: {
            messages: host.room.messages.length,
            openBroadcastId: host.room.openBroadcastId,
            moderationRequired: host.room.moderationRequired,
            pendingHumanRequests: pendingHumanRequests(host.room).length,
          },
        }
      : {}),
    ...(host.coordinator
      ? {
          coordinator: {
            ...host.coordinator,
            focused: host.focusedId === ORCHESTRATOR_ID,
            key: "0" as const,
          },
        }
      : {}),
    workers: host.workers
      .filter(({ record }) =>
        record.activity !== "stopped" && !host.tearingDownWorkerIds.has(record.id),
      )
      .map(({ record }) => ({
        ...record,
        key: String(record.slot - FIRST_WORKER_KEY + 1),
        focused: host.focusedId === record.id,
      })),
    scopedModels: host.scopedModels.map((model) => ({ ...model })),
  };
}

export function installWidget(host: SharedHost, ctx: ExtensionContext): void {
  if (!host.active) return;
  ctx.ui.setWidget("pi-orchestrator", (tui, theme) => {
    const requestRender = () => tui.requestRender();
    const unsubscribe = subscribe(host, requestRender);
    const widget = new OrchestratorWidget(theme, () => snapshot(host), requestRender);
    return {
      render: (width: number) => widget.render(width),
      invalidate: () => widget.invalidate(),
      dispose: () => {
        unsubscribe();
        widget.dispose();
      },
    };
  }, { placement: "belowEditor" });
}

export async function showWorkerPicker(host: SharedHost, ctx: ExtensionContext): Promise<void> {
  if (!host.active) throw new Error("Run /orchestrate first.");
  const selected = await ctx.ui.custom<string | null>((tui, theme, _keybindings, done) => {
    const current = snapshot(host);
    const items = [
      {
        value: ORCHESTRATOR_ID,
        label: "0  orchestrator",
        description: current.coordinator?.activity ?? "idle",
      },
      ...current.workers
        .filter((worker) => worker.activity === "idle" || worker.activity === "working")
        .map((worker) => ({
          value: worker.id,
          label: `${worker.key}  ${worker.name}`,
          description: `${worker.activity} · ${worker.model.provider}/${worker.model.id}`,
        })),
    ];
    const selectList = new SelectList(items, items.length, {
      selectedPrefix: (text) => theme.fg("accent", text),
      selectedText: (text) => theme.fg("accent", text),
      description: (text) => theme.fg("muted", text),
      scrollInfo: (text) => theme.fg("dim", text),
      noMatch: (text) => theme.fg("warning", text),
    });
    selectList.setSelectedIndex(preferredSwitcherIndex(current, host.previousFocusedId));
    selectList.onSelect = (item) => done(item.value);
    selectList.onCancel = () => done(null);

    return {
      render(width: number) {
        return [
          theme.fg("accent", theme.bold("Switch session")),
          ...selectList.render(width),
          theme.fg("dim", "0–8 select · ↑↓ navigate · enter confirm · esc cancel"),
        ];
      },
      invalidate() {
        selectList.invalidate();
      },
      handleInput(data: string) {
        const target = switchTargetForKey(data, snapshot(host));
        if (target) done(target);
        else selectList.handleInput(data);
        tui.requestRender();
      },
    };
  });
  if (selected) await activateFromContext(host, ctx, selected);
}

export async function prepareCoordinatorSessionSwitch(
  host: SharedHost,
  ctx: ExtensionContext,
): Promise<void> {
  if (!host.active || ownerIdForContext(host, ctx) !== ORCHESTRATOR_ID) return;
  host.persist?.();
  host.persist = null;
  const workers = [...host.workers];
  for (const worker of workers) {
    host.tearingDownWorkerIds.add(worker.record.id);
    worker.record.activity = "stopped";
  }

  try {
    await runSerializedActivation(host.activation, async () => {
      let failure: unknown;
      try {
        if (host.focusedId !== ORCHESTRATOR_ID) resumeCoordinatorTui(host);
        for (const worker of workers) {
          try {
            await worker.handle?.dispose();
          } catch (value) {
            failure ??= value;
          }
        }
      } finally {
        clearRuntimeState(host);
      }
      if (failure) throw failure;
    });
  } finally {
    ctx.ui.setWidget("pi-orchestrator", undefined);
    notify(host);
  }
}

export async function deactivateOrchestrator(
  host: SharedHost,
  ctx: ExtensionContext,
  writeEntry?: PersistenceWriter,
): Promise<void> {
  assertCoordinatorContext(host, ctx);
  for (const worker of [...host.workers]) {
    try {
      await stopWorker(host, worker.record.id);
    } catch (value) {
      ctx.ui.notify(
        `Could not stop worker ${worker.record.name}: ${value instanceof Error ? value.message : String(value)}`,
        "warning",
      );
    }
  }
  writeEntry?.(ORCHESTRATION_STATE_TYPE, inactivePersistedState());
  clearRuntimeState(host);
  ctx.ui.setWidget("pi-orchestrator", undefined);
  notify(host);
}
