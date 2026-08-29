import assert from "node:assert/strict";
import test from "node:test";
import {
  ORCHESTRATION_STATE_TYPE,
  createPersistedState,
  findPersistedState,
} from "../src/persistence.js";
import { createRoomState, postRoomMessage } from "../src/room.js";
import type { WorkerRecord } from "../src/types.js";

const worker: WorkerRecord = {
  id: "worker-1",
  slot: 5,
  name: "reviewer",
  cwd: "/repo",
  model: { provider: "xai", id: "grok-4.5" },
  thinkingLevel: "high",
  activity: "working",
  sessionId: "worker-session",
  sessionFile: "/sessions/worker.jsonl",
  createdAt: 1,
  lastActivityAt: 2,
  readCursor: "entry-1",
  unreadCount: 3,
};

const scope = [
  { provider: "xai", id: "grok-4.5", thinkingLevel: "high" as const },
  { provider: "openai-codex", id: "gpt-5.6-sol", thinkingLevel: "high" as const },
];

test("persisted orchestration state retains the worker list and resumable session paths", () => {
  const state = createPersistedState(scope, [worker], "worker-1");

  assert.equal(state.active, true);
  assert.equal(state.version, 2);
  assert.equal(state.mode, "silo");
  assert.deepEqual(state.scopedModels, scope);
  assert.equal(state.previousFocusedId, "worker-1");
  assert.deepEqual(state.workers, [{
    id: "worker-1",
    slot: 5,
    name: "reviewer",
    cwd: "/repo",
    model: { provider: "xai", id: "grok-4.5" },
    thinkingLevel: "high",
    sessionId: "worker-session",
    sessionFile: "/sessions/worker.jsonl",
    createdAt: 1,
    readCursor: "entry-1",
    unreadCount: 3,
  }]);
});

test("the latest state on the active branch controls automatic restoration", () => {
  const active = createPersistedState(scope, [worker]);
  const entries = [
    { type: "custom", customType: ORCHESTRATION_STATE_TYPE, data: active },
    { type: "custom", customType: "other-extension", data: { active: true } },
    { type: "custom", customType: ORCHESTRATION_STATE_TYPE, data: { version: 1, active: false } },
  ];

  assert.deepEqual(findPersistedState(entries), { version: 2, active: false });
  assert.equal(findPersistedState([{ type: "custom", customType: ORCHESTRATION_STATE_TYPE, data: { nope: true } }]), null);
});

test("an invalid latest state falls back to an older valid snapshot", () => {
  const active = createPersistedState(scope, [worker]);
  let invalidCount = 0;
  const restored = findPersistedState([
    { type: "custom", customType: ORCHESTRATION_STATE_TYPE, data: active },
    { type: "custom", customType: ORCHESTRATION_STATE_TYPE, data: { version: 2, active: true } },
  ], () => { invalidCount++; });

  assert.deepEqual(restored, active);
  assert.equal(invalidCount, 1);
});

test("room mode persists its ordered log and open broadcast barrier", () => {
  const room = createRoomState();
  postRoomMessage(room, {
    sender: { id: "__orchestrator__", name: "orchestrator", kind: "orchestrator" },
    to: ["all"],
    text: "Deliberate.",
    workers: [{ id: worker.id, name: worker.name, activity: "idle" }],
    id: () => "broadcast-1",
  });

  const state = createPersistedState(scope, [worker], undefined, "room", room);
  const restored = findPersistedState([
    { type: "custom", customType: ORCHESTRATION_STATE_TYPE, data: state },
  ]);

  assert.equal(restored?.active, true);
  if (!restored?.active) return;
  assert.equal(restored.mode, "room");
  assert.equal(restored.room?.openBroadcastId, "broadcast-1");
  assert.equal(restored.room?.messages[0]?.text, "Deliberate.");
});

test("invalid room counters are rejected during restoration", () => {
  const room = createRoomState();
  const state = createPersistedState(scope, [worker], undefined, "room", room);
  assert.equal(state.active, true);
  if (!state.active || !state.room) return;

  const invalidSequence = structuredClone(state);
  invalidSequence.room!.nextSequence = 0;
  const negativeCount = structuredClone(state);
  negativeCount.room!.messagesSinceModeration = -1;

  assert.equal(findPersistedState([
    { type: "custom", customType: ORCHESTRATION_STATE_TYPE, data: invalidSequence },
  ]), null);
  assert.equal(findPersistedState([
    { type: "custom", customType: ORCHESTRATION_STATE_TYPE, data: negativeCount },
  ]), null);
});

test("version one sessions migrate to silo mode", () => {
  const current = createPersistedState(scope, [worker]);
  assert.equal(current.active, true);
  if (!current.active) return;
  const legacy = {
    version: 1,
    active: true,
    scopedModels: scope,
    workers: current.workers,
  };

  const restored = findPersistedState([
    { type: "custom", customType: ORCHESTRATION_STATE_TYPE, data: legacy },
  ]);

  assert.equal(restored?.active, true);
  if (restored?.active) assert.equal(restored.mode, "silo");
});
