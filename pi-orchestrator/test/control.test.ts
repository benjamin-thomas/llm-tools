import assert from "node:assert/strict";
import test from "node:test";
import type { ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { Model } from "@earendil-works/pi-ai";
import {
  assertCoordinator,
  reconcileThinkingLevelForModel,
  updateModel,
  type SharedHost,
} from "../src/host.js";
import {
  ORCHESTRATOR_ID,
  SHARED_STATE_VERSION,
  type CoordinatorRecord,
  type ScopedModelSpec,
  type WorkerRecord,
} from "../src/types.js";

test("coordinator-only operations reject calls from workers", () => {
  assert.doesNotThrow(() => assertCoordinator("session-a", "session-a"));
  assert.throws(() => assertCoordinator("session-b", "session-a"), /only available to the orchestrator/);
});

test("reconcileThinkingLevelForModel leaves a supported level unchanged", () => {
  const model = makeModel({ reasoning: true, thinkingLevelMap: { off: "off", high: "high" } });
  assert.equal(reconcileThinkingLevelForModel(model, "high"), "high");
  assert.equal(reconcileThinkingLevelForModel(model, "off"), "off");
});

test("reconcileThinkingLevelForModel clamps an unsupported level down to the nearest supported one", () => {
  // Model supports only up to "high"; "xhigh" and "max" are not mapped.
  const model = makeModel({ reasoning: true, thinkingLevelMap: { off: "off", high: "high" } });
  assert.equal(reconcileThinkingLevelForModel(model, "xhigh"), "high");
  assert.equal(reconcileThinkingLevelForModel(model, "max"), "high");
});

test("reconcileThinkingLevelForModel resets to off for a non-reasoning model", () => {
  const model = makeModel({ reasoning: false });
  assert.equal(reconcileThinkingLevelForModel(model, "high"), "off");
  assert.equal(reconcileThinkingLevelForModel(model, "xhigh"), "off");
});

test("updateModel reconciles the worker's thinking level to the new model", () => {
  const oldSpec: ScopedModelSpec = { provider: "anthropic", id: "claude-opus-4-7" };
  const worker: WorkerRecord = {
    id: "worker-1",
    slot: 5,
    name: "opus",
    cwd: "/repo",
    model: oldSpec,
    thinkingLevel: "xhigh",
    activity: "idle",
    sessionId: "worker-session",
    sessionFile: "/sessions/worker.jsonl",
    createdAt: 1,
    lastActivityAt: 1,
    readCursor: null,
    unreadCount: 0,
  };
  const host = makeHost(worker);
  // The new model only supports up to "high" (no "xhigh"/"max").
  const newModel = makeModel({
    id: "claude-haiku-4-5",
    name: "Claude Haiku 4.5",
    api: "anthropic-messages",
    provider: "anthropic",
    reasoning: true,
  });

  updateModel(host, makeWorkerContext(worker), newModel);

  assert.equal(host.workers[0]!.record.model.provider, "anthropic");
  assert.equal(host.workers[0]!.record.model.id, "claude-haiku-4-5");
  assert.equal(host.workers[0]!.record.thinkingLevel, "high");
});

test("updateModel reconciles the coordinator's thinking level to the new model", () => {
  const oldSpec: ScopedModelSpec = { provider: "anthropic", id: "claude-opus-4-7" };
  const coordinator: CoordinatorRecord = {
    id: ORCHESTRATOR_ID,
    name: "orchestrator",
    cwd: "/repo",
    model: oldSpec,
    thinkingLevel: "xhigh",
    activity: "idle",
    sessionId: "coordinator-session",
    lastActivityAt: 1,
  };
  const host = makeCoordinatorHost(coordinator);
  // The new model only supports up to "high" (no "xhigh"/"max").
  const newModel = makeModel({
    id: "claude-haiku-4-5",
    name: "Claude Haiku 4.5",
    api: "anthropic-messages",
    provider: "anthropic",
    reasoning: true,
  });

  updateModel(host, makeCoordinatorContext(coordinator), newModel);

  assert.equal(host.coordinator?.model?.provider, "anthropic");
  assert.equal(host.coordinator?.model?.id, "claude-haiku-4-5");
  assert.equal(host.coordinator?.thinkingLevel, "high");
});

function makeModel(overrides: Partial<Model<"anthropic-messages">> = {}): Model<"anthropic-messages"> {
  return {
    id: "test-model",
    name: "Test Model",
    api: "anthropic-messages",
    provider: "anthropic",
    baseUrl: "https://api.anthropic.com",
    reasoning: true,
    input: ["text"],
    cost: { input: 1, output: 5, cacheRead: 0.1, cacheWrite: 1.25 },
    contextWindow: 200000,
    maxTokens: 64000,
    ...overrides,
  } as Model<"anthropic-messages">;
}

function makeHost(worker: WorkerRecord): SharedHost {
  return {
    version: SHARED_STATE_VERSION,
    active: true,
    focusedId: worker.id,
    previousFocusedId: null,
    coordinator: null,
    scopedModels: [],
    workers: [{ record: worker, handle: null }],
    subscribers: new Set(),
    activation: { inProgress: null, queuedTarget: null },
    parentTui: null,
    parentDone: null,
    parentHandoffActive: false,
    persist: null,
    lastPersistedState: null,
  };
}

function makeCoordinatorHost(coordinator: CoordinatorRecord): SharedHost {
  return {
    version: SHARED_STATE_VERSION,
    active: true,
    focusedId: ORCHESTRATOR_ID,
    previousFocusedId: null,
    coordinator,
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

function makeWorkerContext(worker: WorkerRecord): ExtensionContext {
  return {
    sessionManager: {
      getSessionId: () => worker.sessionId ?? "",
      getSessionFile: () => worker.sessionFile,
    },
  } as unknown as ExtensionContext;
}

function makeCoordinatorContext(coordinator: CoordinatorRecord): ExtensionContext {
  return {
    sessionManager: {
      getSessionId: () => coordinator.sessionId,
      getSessionFile: () => coordinator.sessionFile,
    },
  } as unknown as ExtensionContext;
}
