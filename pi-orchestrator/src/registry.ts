import { randomUUID } from "node:crypto";
import {
  FIRST_WORKER_KEY,
  LAST_WORKER_KEY,
  type ScopedModelSpec,
  type SpawnRequest,
  type WorkerRecord,
} from "./types.js";

export type IdSource = () => string;

const RESERVED_WORKER_NAMES = new Set(["all", "human", "orchestrator"]);

export function configuredModelOrder(
  scope: readonly ScopedModelSpec[],
): ScopedModelSpec[] {
  return scope.map((model) => ({ ...model }));
}

export function defaultModelForSlot(
  modelOrder: readonly ScopedModelSpec[],
  slot: number,
): ScopedModelSpec {
  if (modelOrder.length === 0) throw new Error("No scoped models are configured.");
  if (!Number.isInteger(slot) || slot < FIRST_WORKER_KEY || slot > LAST_WORKER_KEY) {
    throw new Error("Worker slot is outside the supported 1–8 switcher range.");
  }
  return modelOrder[(slot - FIRST_WORKER_KEY) % modelOrder.length]!;
}

export function modelKey(model: Pick<ScopedModelSpec, "provider" | "id">): string {
  return `${model.provider}/${model.id}`;
}

export function validateWorkerName(name: string): string {
  const trimmed = name.trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(trimmed)) {
    throw new Error("Worker names must be 1-64 characters using letters, numbers, '.', '_' or '-'.");
  }
  if (RESERVED_WORKER_NAMES.has(trimmed.toLowerCase())) {
    throw new Error(`Worker name is reserved for room addressing: ${trimmed}`);
  }
  return trimmed;
}

export function renameWorker(workers: WorkerRecord[], workerId: string, requestedName: string): WorkerRecord {
  const worker = workers.find((candidate) => candidate.id === workerId);
  if (!worker) throw new Error(`Unknown worker: ${workerId}`);

  const name = validateWorkerName(requestedName);
  const duplicate = workers.some(
    (candidate) => candidate.id !== workerId && candidate.name.toLowerCase() === name.toLowerCase(),
  );
  if (duplicate) throw new Error(`Worker name already exists: ${name}`);

  worker.name = name;
  worker.lastActivityAt = Date.now();
  return worker;
}

export function resolveSpawnModels(
  scope: readonly ScopedModelSpec[],
  request: SpawnRequest,
): ScopedModelSpec[] {
  if (scope.length === 0) {
    throw new Error("No scoped models are configured. Use /scoped-models before orchestrating.");
  }

  const byKey = new Map(scope.map((model) => [modelKey(model), model]));
  if (request.models?.length) {
    if (request.count !== undefined && request.count !== request.models.length) {
      throw new Error("When count and models are both provided, count must match models.length.");
    }
    const seen = new Set<string>();
    return request.models.map((requested) => {
      const model = byKey.get(requested);
      if (!model) throw new Error(`Model is not in the scoped models: ${requested}`);
      if (seen.has(requested)) throw new Error(`Duplicate model requested: ${requested}`);
      seen.add(requested);
      return model;
    });
  }

  const count = request.count;
  if (!Number.isInteger(count) || count === undefined || count < 1) {
    throw new Error("A positive worker count is required when models are not specified.");
  }
  if (count > LAST_WORKER_KEY - FIRST_WORKER_KEY + 1) {
    throw new Error("The first slice supports at most 8 workers.");
  }

  return Array.from({ length: count }, (_, index) =>
    defaultModelForSlot(scope, FIRST_WORKER_KEY + index),
  );
}

function modelDerivedName(model: ScopedModelSpec): string {
  const raw = model.id.split("/").filter(Boolean).at(-1) ?? model.id;
  return validateWorkerName(raw.replace(/[^A-Za-z0-9._-]/g, "-"));
}

export function uniqueModelName(workers: readonly WorkerRecord[], model: ScopedModelSpec): string {
  const base = modelDerivedName(model);
  const used = new Set(workers.map((worker) => worker.name.toLowerCase()));
  if (!used.has(base.toLowerCase())) return base;
  for (let suffix = 2; suffix < 10_000; suffix++) {
    const suffixText = `-${suffix}`;
    const candidate = `${base.slice(0, 64 - suffixText.length)}${suffixText}`;
    if (!used.has(candidate.toLowerCase())) return candidate;
  }
  throw new Error(`Could not allocate a unique name for ${modelKey(model)}`);
}

export function nextWorkerSlot(workers: readonly WorkerRecord[]): number {
  const used = new Set(workers.map((worker) => worker.slot));
  for (let slot = FIRST_WORKER_KEY; slot <= LAST_WORKER_KEY; slot++) {
    if (!used.has(slot)) return slot;
  }
  throw new Error("All 8 worker switcher slots are in use.");
}

export function assertWorkerCanReset(isIdle: boolean): void {
  if (!isIdle) throw new Error("Reset requires an idle worker.");
}

export function prepareWorkerForReset(worker: WorkerRecord, now = Date.now()): void {
  worker.activity = "creating";
  worker.lastActivityAt = now;
  worker.readCursor = null;
  worker.unreadCount = 0;
  delete worker.sessionId;
  delete worker.sessionFile;
  delete worker.error;
}

export function createWorkerRecord(
  workers: readonly WorkerRecord[],
  model: ScopedModelSpec,
  cwd: string,
  options: { id?: IdSource; now?: () => number } = {},
): WorkerRecord {
  const now = (options.now ?? Date.now)();
  return {
    id: (options.id ?? randomUUID)(),
    slot: nextWorkerSlot(workers),
    name: uniqueModelName(workers, model),
    cwd,
    model: { ...model },
    thinkingLevel: model.thinkingLevel ?? "medium",
    activity: "creating",
    createdAt: now,
    lastActivityAt: now,
    readCursor: null,
    unreadCount: 0,
  };
}
