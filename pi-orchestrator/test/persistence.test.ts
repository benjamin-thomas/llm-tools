import assert from "node:assert/strict";
import test from "node:test";
import {
  ORCHESTRATION_STATE_TYPE,
  createPersistedState,
  findPersistedState,
} from "../src/persistence.js";
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

  assert.deepEqual(findPersistedState(entries), { version: 1, active: false });
  assert.equal(findPersistedState([{ type: "custom", customType: ORCHESTRATION_STATE_TYPE, data: { nope: true } }]), null);
});
