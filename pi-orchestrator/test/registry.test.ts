import assert from "node:assert/strict";
import test from "node:test";
import {
  createWorkerRecord,
  defaultModelForSlot,
  assertWorkerCanReset,
  modelKey,
  prepareWorkerForReset,
  renameWorker,
  resolveSpawnModels,
  configuredModelOrder,
  uniqueModelName,
} from "../src/registry.js";
import type { ScopedModelSpec, WorkerRecord } from "../src/types.js";

const scope: ScopedModelSpec[] = [
  { provider: "openai-codex", id: "gpt-5.6-sol", thinkingLevel: "high" },
  { provider: "xai", id: "grok-4.5" },
  { provider: "kimi-coding", id: "k3" },
];

test("orchestration preserves the configured scoped model order", () => {
  const ordered = configuredModelOrder(scope);

  assert.deepEqual(ordered.map(modelKey), [
    "openai-codex/gpt-5.6-sol",
    "xai/grok-4.5",
    "kimi-coding/k3",
  ]);
  assert.notEqual(ordered, scope, "the host should own its model-order array");
});

test("default worker models cycle through configured order by stable switcher position", () => {
  assert.equal(modelKey(defaultModelForSlot(scope, 5)), "openai-codex/gpt-5.6-sol");
  assert.equal(modelKey(defaultModelForSlot(scope, 6)), "xai/grok-4.5");
  assert.equal(modelKey(defaultModelForSlot(scope, 7)), "kimi-coding/k3");
  assert.equal(modelKey(defaultModelForSlot(scope, 8)), "openai-codex/gpt-5.6-sol");

  const selected = resolveSpawnModels(scope, { count: 5 });
  assert.deepEqual(selected.map(modelKey), [
    "openai-codex/gpt-5.6-sol",
    "xai/grok-4.5",
    "kimi-coding/k3",
    "openai-codex/gpt-5.6-sol",
    "xai/grok-4.5",
  ]);
});

test("specific spawning rejects models outside scope and duplicates", () => {
  assert.throws(
    () => resolveSpawnModels(scope, { models: ["anthropic/opus"] }),
    /not in the scoped models/,
  );
  assert.throws(
    () => resolveSpawnModels(scope, { models: ["xai/grok-4.5", "xai/grok-4.5"] }),
    /Duplicate model/,
  );
});

test("default spawning rejects more workers than shortcut positions", () => {
  assert.throws(() => resolveSpawnModels(scope, { count: 9 }), /at most 8 workers/);
});

test("worker IDs and switcher slots remain stable across rename", () => {
  const worker = createWorkerRecord([], scope[0]!, "/repo", {
    id: () => "stable-id",
    now: () => 42,
  });
  const originalSlot = worker.slot;
  renameWorker([worker], worker.id, "reviewer");
  assert.equal(worker.id, "stable-id");
  assert.equal(worker.slot, originalSlot);
  assert.equal(worker.name, "reviewer");
});

test("model-derived names are disambiguated", () => {
  const first = createWorkerRecord([], scope[1]!, "/repo", { id: () => "one" });
  const second = createWorkerRecord([first], scope[1]!, "/repo", { id: () => "two" });
  assert.equal(first.name, "grok-4.5");
  assert.equal(second.name, "grok-4.5-2");
});

test("rename rejects duplicate, invalid, and unknown names", () => {
  const workers = [
    createWorkerRecord([], scope[0]!, "/repo", { id: () => "one" }),
    createWorkerRecord([], scope[1]!, "/repo", { id: () => "two" }),
  ] as WorkerRecord[];
  workers[0]!.name = "builder";
  workers[1]!.name = "reviewer";

  assert.throws(() => renameWorker(workers, "two", "BUILDER"), /already exists/);
  assert.throws(() => renameWorker(workers, "two", "bad name"), /Worker names/);
  assert.throws(() => renameWorker(workers, "missing", "valid"), /Unknown worker/);
});

test("worker creation uses the scoped thinking level and defaults to medium", () => {
  const explicit = createWorkerRecord([], scope[0]!, "/exact/coordinator/cwd", {
    id: () => "explicit-worker",
  });
  const defaulted = createWorkerRecord([explicit], scope[1]!, "/exact/coordinator/cwd", {
    id: () => "default-worker",
  });

  assert.equal(explicit.cwd, "/exact/coordinator/cwd");
  assert.equal(explicit.thinkingLevel, "high");
  assert.equal(defaulted.thinkingLevel, "medium");
});

test("reset preserves worker identity and configuration while clearing session state", () => {
  const worker = createWorkerRecord([], scope[0]!, "/repo", {
    id: () => "stable-worker",
    now: () => 1,
  });
  worker.name = "reviewer";
  worker.model = { provider: "xai", id: "grok-4.5" };
  worker.thinkingLevel = "high";
  worker.activity = "idle";
  worker.sessionId = "old-session";
  worker.sessionFile = "/old.jsonl";
  worker.readCursor = "old-entry";
  worker.unreadCount = 2;
  worker.error = "old error";

  prepareWorkerForReset(worker, 99);

  assert.deepEqual(
    {
      id: worker.id,
      slot: worker.slot,
      name: worker.name,
      cwd: worker.cwd,
      model: worker.model,
      thinkingLevel: worker.thinkingLevel,
    },
    {
      id: "stable-worker",
      slot: 5,
      name: "reviewer",
      cwd: "/repo",
      model: { provider: "xai", id: "grok-4.5" },
      thinkingLevel: "high",
    },
  );
  assert.equal(worker.activity, "creating");
  assert.equal(worker.sessionId, undefined);
  assert.equal(worker.sessionFile, undefined);
  assert.equal(worker.readCursor, null);
  assert.equal(worker.unreadCount, 0);
  assert.equal(worker.error, undefined);
  assert.equal(worker.lastActivityAt, 99);
});

test("reset rejects a worker that is still busy", () => {
  assert.doesNotThrow(() => assertWorkerCanReset(true));
  assert.throws(() => assertWorkerCanReset(false), /idle worker/);
});

test("duplicate model-derived names stay within the worker name limit", () => {
  const max64 = "a".repeat(64);
  const model: ScopedModelSpec = { provider: "test", id: "prefix/" + max64 };
  const occupied = createWorkerRecord([], model, "/repo", { id: () => "one" });
  assert.equal(occupied.name.length, 64, "occupied name must be exactly 64 chars");

  const collision = uniqueModelName([occupied], model);
  assert.ok(
    collision.length <= 64,
    `collision name "${collision}" is ${collision.length} chars, expected ≤ 64`,
  );
  assert.ok(
    /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(collision),
    `collision name "${collision}" must match worker name regex`,
  );
  assert.notEqual(collision, occupied.name);
  assert.ok(/-\d+$/.test(collision), "collision name must retain a numeric suffix");
});