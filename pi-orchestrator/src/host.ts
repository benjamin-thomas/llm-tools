import type { ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { ThinkingLevel } from "@earendil-works/pi-agent-core";
import { clampThinkingLevel, type Model } from "@earendil-works/pi-ai";
import { SelectList, type Component } from "@earendil-works/pi-tui";
import { requestSerializedActivation, type ActivationQueue } from "./activation.js";
import { assertSupportedPiVersion } from "./compatibility.js";
import {
  captureDispatchReceipt,
  countUnreadResponses,
  dispatchToWorker,
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
import { createWorkerHandle, type WorkerHandle } from "./runtime.js";
import {
  FIRST_WORKER_KEY,
  ORCHESTRATOR_ID,
  SHARED_STATE_VERSION,
  type CoordinatorRecord,
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

const SHARED_KEY = Symbol.for("pi-orchestrator.host.v2");

function newHost(): SharedHost {
  return {
    version: SHARED_STATE_VERSION,
    active: false,
    focusedId: ORCHESTRATOR_ID,
    previousFocusedId: null,
    coordinator: null,
    scopedModels: [],
    workers: [],
    subscribers: new Set(),
    activation: { inProgress: null, queuedTarget: null },
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
  host.active = false;
  host.focusedId = ORCHESTRATOR_ID;
  host.previousFocusedId = null;
  host.coordinator = null;
  host.scopedModels = [];
  host.workers = [];
  host.parentTui = null;
  host.parentDone = null;
  host.parentHandoffActive = false;
  host.persist = null;
  host.lastPersistedState = null;
}

export function activateOrchestrator(
  ctx: ExtensionContext,
  writeEntry?: PersistenceWriter,
): SharedHost {
  assertSupportedPiVersion();
  const host = getHost();
  if (ctx.scopedModels.length === 0) {
    throw new Error("No scoped models are configured. Configure /scoped-models first.");
  }

  const now = Date.now();
  const sessionFile = ctx.sessionManager.getSessionFile();
  const model = ctx.model ? scopedSpec(ctx.model) : undefined;
  host.active = true;
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
  if (!state.active) return host;

  host.active = true;
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
            worker.record.activity = "error";
            worker.record.error = error.message;
            worker.record.lastActivityAt = Date.now();
            notify(host);
          },
        },
        { resumeExistingSession: true },
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
  if (worker) return worker.record.id;

  // Session replacement changes identity before the new extension instance starts.
  if (host.active && host.focusedId === ORCHESTRATOR_ID && host.coordinator) {
    host.coordinator.sessionId = sessionId;
    if (sessionFile !== undefined) host.coordinator.sessionFile = sessionFile;
    return ORCHESTRATOR_ID;
  }
  const focusedWorker = host.workers.find(({ record }) => record.id === host.focusedId);
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
            record.activity = "error";
            record.error = error.message;
            record.lastActivityAt = Date.now();
            notify(host);
          },
        },
      );
      record.activity = "idle";
      record.lastActivityAt = Date.now();
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

function requireLiveWorker(host: SharedHost, workerId: string): RuntimeWorker & { handle: WorkerHandle } {
  const worker = host.workers.find(({ record }) => record.id === workerId);
  if (!worker) throw new Error(`Unknown worker: ${workerId}`);
  if (!worker.handle || worker.record.activity === "error" || worker.record.activity === "stopped") {
    throw new Error(`Worker is unavailable: ${workerId}`);
  }
  return worker as RuntimeWorker & { handle: WorkerHandle };
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
          worker.record.error = error.message;
          notify(host);
        },
        onSettled: () => {
          worker.record.activity = "idle";
          worker.record.lastActivityAt = Date.now();
          refreshUnread(worker);
          notify(host);
        },
      },
    );
    return { ...receipt, ...acknowledgement };
  } catch (error) {
    worker.record.activity = "idle";
    worker.record.lastActivityAt = Date.now();
    notify(host);
    throw error;
  }
}

export async function waitForLiveWorker(
  host: SharedHost,
  ctx: ExtensionContext,
  workerId: string,
): Promise<void> {
  assertCoordinatorContext(host, ctx);
  const worker = requireLiveWorker(host, workerId);
  await waitForWorker(worker.handle.runtime.session as unknown as WaitSession);
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
  worker.record.readCursor = result.cursor;
  refreshUnread(worker);
  notify(host);
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

export async function resetWorker(
  host: SharedHost,
  ctx: ExtensionContext,
  workerId: string,
): Promise<WorkerRecord> {
  assertCoordinatorContext(host, ctx);
  const worker: RuntimeWorker | undefined = host.workers.find(
    ({ record }) => record.id === workerId,
  );
  if (!worker?.handle) throw new Error(`Worker ${workerId} is unavailable.`);
  const previousHandle = worker.handle;
  assertWorkerCanReset(previousHandle.runtime.session.isIdle);

  await previousHandle.dispose();
  worker.handle = null;
  prepareWorkerForReset(worker.record);
  notify(host);

  try {
    worker.handle = await createWorkerHandle(
      worker.record,
      () => host.scopedModels,
      {
        isFocused: () => host.focusedId === worker.record.id,
        onState: () => notify(host),
        onError: (error) => {
          worker.record.activity = "error";
          worker.record.error = error.message;
          worker.record.lastActivityAt = Date.now();
          notify(host);
        },
      },
    );
    worker.record.activity = "idle";
    worker.record.lastActivityAt = Date.now();
    notify(host);
    return worker.record;
  } catch (value) {
    const error = value instanceof Error ? value : new Error(String(value));
    worker.record.activity = "error";
    worker.record.error = error.message;
    notify(host);
    throw error;
  }
}

export async function stopWorker(host: SharedHost, workerId: string): Promise<void> {
  const index = host.workers.findIndex(({ record }) => record.id === workerId);
  if (index < 0) throw new Error(`Unknown worker: ${workerId}`);
  const worker = host.workers[index]!;
  if (host.focusedId === workerId) await activate(host, ORCHESTRATOR_ID);
  worker.record.activity = "stopped";
  await worker.handle?.dispose();
  host.workers.splice(index, 1);
  if (host.previousFocusedId === workerId) host.previousFocusedId = null;
  notify(host);
}

async function doActivate(host: SharedHost, targetId: string): Promise<void> {
  if (targetId === host.focusedId) return;
  const target = targetId === ORCHESTRATOR_ID
    ? null
    : host.workers.find(({ record }) => record.id === targetId);
  if (targetId !== ORCHESTRATOR_ID && (!target?.handle || target.record.activity === "error")) {
    throw new Error(`Worker is unavailable: ${targetId}`);
  }

  const priorFocusedId = host.focusedId;
  const current = host.workers.find(({ record }) => record.id === priorFocusedId);
  current?.handle?.suspend();

  if (targetId === ORCHESTRATOR_ID) {
    host.previousFocusedId = priorFocusedId;
    host.focusedId = ORCHESTRATOR_ID;
    host.parentTui?.start();
    host.parentTui?.requestRender(true);
    const done = host.parentDone;
    host.parentTui = null;
    host.parentDone = null;
    host.parentHandoffActive = false;
    notify(host);
    done?.();
    return;
  }

  host.previousFocusedId = priorFocusedId;
  host.focusedId = targetId;
  if (target!.handle!.started) target!.handle!.resume();
  else target!.handle!.start();
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
    if (worker) {
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
    focusedId: host.focusedId,
    ...(host.coordinator
      ? {
          coordinator: {
            ...host.coordinator,
            focused: host.focusedId === ORCHESTRATOR_ID,
            key: "0" as const,
          },
        }
      : {}),
    workers: host.workers.map(({ record }) => ({
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
      ...current.workers.map((worker) => ({
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
  for (const worker of host.workers) await worker.handle?.dispose();
  clearRuntimeState(host);
  ctx.ui.setWidget("pi-orchestrator", undefined);
  notify(host);
}

export async function deactivateOrchestrator(
  host: SharedHost,
  ctx: ExtensionContext,
  writeEntry?: PersistenceWriter,
): Promise<void> {
  assertCoordinatorContext(host, ctx);
  for (const worker of [...host.workers]) await stopWorker(host, worker.record.id);
  writeEntry?.(ORCHESTRATION_STATE_TYPE, inactivePersistedState());
  clearRuntimeState(host);
  ctx.ui.setWidget("pi-orchestrator", undefined);
  notify(host);
}
