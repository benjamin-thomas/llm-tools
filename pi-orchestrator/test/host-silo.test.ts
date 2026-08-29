import assert from "node:assert/strict";
import test from "node:test";
import type { ExtensionContext } from "@earendil-works/pi-coding-agent";
import { createActivationQueue } from "../src/activation.js";
import type { SessionEntryLike } from "../src/communication.js";
import {
  readLiveWorker,
  unreadInbox,
  type SharedHost,
} from "../src/host.js";
import type { WorkerHandle } from "../src/runtime.js";
import {
  ORCHESTRATOR_ID,
  SHARED_STATE_VERSION,
  type WorkerRecord,
} from "../src/types.js";

const siloBranch: SessionEntryLike[] = [
  { type: "message", id: "e1", timestamp: "t1", message: { role: "user", content: "First." } },
  { type: "message", id: "e2", timestamp: "t2", message: { role: "assistant", stopReason: "stop", content: "First reply." } },
  { type: "message", id: "e3", timestamp: "t3", message: { role: "user", content: "Second." } },
  { type: "message", id: "e4", timestamp: "t4", message: { role: "assistant", stopReason: "stop", content: "Second reply." } },
];

test("explicit historical silo reads do not rewind the automatic cursor or replay replies", () => {
  const host = makeSiloHost(siloBranch);
  const ctx = coordinatorContext();

  const consumed = readLiveWorker(host, ctx, "worker-1", {});
  assert.equal(consumed.cursor, "e4");
  assert.equal(host.workers[0]!.record.readCursor, "e4");
  assert.equal(host.workers[0]!.record.unreadCount, 0);

  const historical = readLiveWorker(host, ctx, "worker-1", { after: "e1", limit: 1 });
  assert.deepEqual(historical.messages.map((entry) => entry.id), ["e2"]);
  assert.equal(historical.cursor, "e2");
  assert.equal(host.workers[0]!.record.readCursor, "e4");
  assert.equal(host.workers[0]!.record.unreadCount, 0);
  assert.equal(historical.unreadRemaining, 0);
  assert.deepEqual(unreadInbox(host, ctx), []);

  const next = readLiveWorker(host, ctx, "worker-1", {});
  assert.deepEqual(next.messages, []);
  assert.equal(next.cursor, "e4");
  assert.equal(host.workers[0]!.record.readCursor, "e4");
});

test("explicit silo reads ahead of the automatic cursor are non-advancing peeks", () => {
  const host = makeSiloHost(siloBranch);
  const ctx = coordinatorContext();

  const consumed = readLiveWorker(host, ctx, "worker-1", { limit: 2 });
  assert.equal(consumed.cursor, "e2");
  assert.equal(host.workers[0]!.record.readCursor, "e2");
  assert.equal(host.workers[0]!.record.unreadCount, 1);

  const peek = readLiveWorker(host, ctx, "worker-1", { after: "e3" });
  assert.deepEqual(peek.messages.map((entry) => entry.id), ["e4"]);
  assert.equal(peek.cursor, "e4");
  assert.equal(host.workers[0]!.record.readCursor, "e2");
  assert.equal(host.workers[0]!.record.unreadCount, 1);
  assert.equal(peek.unreadRemaining, 1);

  const next = readLiveWorker(host, ctx, "worker-1", {});
  assert.deepEqual(next.messages.map((entry) => entry.id), ["e3", "e4"]);
  assert.equal(host.workers[0]!.record.readCursor, "e4");
  assert.equal(host.workers[0]!.record.unreadCount, 0);
});

test("a stale explicit silo cursor does not overwrite a valid automatic cursor", () => {
  const replacement: SessionEntryLike[] = [
    { type: "message", id: "n1", timestamp: "t5", message: { role: "user", content: "Fresh." } },
    { type: "message", id: "n2", timestamp: "t6", message: { role: "assistant", stopReason: "stop", content: "New reply." } },
  ];
  const host = makeSiloHost(replacement, "n2");
  host.workers[0]!.record.unreadCount = 0;

  const recovered = readLiveWorker(host, coordinatorContext(), "worker-1", {
    after: "old-e1",
    limit: 1,
  });
  assert.deepEqual(recovered.messages.map((entry) => entry.id), ["n1"]);
  assert.equal(recovered.cursor, "n1");
  assert.equal(host.workers[0]!.record.readCursor, "n2");
  assert.equal(host.workers[0]!.record.unreadCount, 0);
});

test("implicit silo reads still advance the automatic cursor and recover after /new", () => {
  let branch: SessionEntryLike[] = siloBranch;
  const host = makeSiloHost([], null, () => branch);
  const ctx = coordinatorContext();

  const first = readLiveWorker(host, ctx, "worker-1", { limit: 2 });
  assert.equal(first.cursor, "e2");
  assert.equal(host.workers[0]!.record.readCursor, "e2");
  assert.equal(host.workers[0]!.record.unreadCount, 1);

  const rest = readLiveWorker(host, ctx, "worker-1", {});
  assert.deepEqual(rest.messages.map((entry) => entry.id), ["e3", "e4"]);
  assert.equal(host.workers[0]!.record.readCursor, "e4");
  assert.equal(host.workers[0]!.record.unreadCount, 0);

  branch = [
    { type: "message", id: "n1", timestamp: "t5", message: { role: "user", content: "Fresh." } },
    { type: "message", id: "n2", timestamp: "t6", message: { role: "assistant", stopReason: "stop", content: "New reply." } },
  ];
  const recovered = readLiveWorker(host, ctx, "worker-1", {});
  assert.deepEqual(recovered.messages.map((entry) => entry.id), ["n1", "n2"]);
  assert.equal(host.workers[0]!.record.readCursor, "n2");
  assert.equal(host.workers[0]!.record.unreadCount, 0);
});

function coordinatorContext(): ExtensionContext {
  return {
    sessionManager: {
      getSessionId: () => "coordinator-session",
      getSessionFile: () => undefined,
    },
  } as unknown as ExtensionContext;
}

function makeSiloHost(
  branch: SessionEntryLike[],
  readCursor: string | null = null,
  getBranch: () => SessionEntryLike[] = () => branch,
): SharedHost {
  const worker: WorkerRecord = {
    id: "worker-1",
    slot: 5,
    name: "worker",
    cwd: "/repo",
    model: { provider: "test", id: "worker" },
    thinkingLevel: "off",
    activity: "idle",
    sessionId: "worker-session",
    createdAt: 1,
    lastActivityAt: 1,
    readCursor,
    unreadCount: 0,
  };
  return {
    version: SHARED_STATE_VERSION,
    active: true,
    mode: "silo",
    room: null,
    roomPumps: new Set(),
    tearingDownWorkerIds: new Set(),
    focusedId: ORCHESTRATOR_ID,
    previousFocusedId: null,
    coordinator: {
      id: ORCHESTRATOR_ID,
      name: "orchestrator",
      cwd: "/repo",
      thinkingLevel: "off",
      activity: "idle",
      sessionId: "coordinator-session",
      lastActivityAt: 1,
    },
    scopedModels: [],
    workers: [{ record: worker, handle: fakeHandle(getBranch) }],
    subscribers: new Set(),
    activation: createActivationQueue(),
    parentTui: null,
    parentDone: null,
    parentHandoffActive: false,
    persist: null,
    lastPersistedState: null,
  };
}

function fakeHandle(getBranch: () => SessionEntryLike[]): WorkerHandle {
  return {
    started: true,
    state: "active",
    start() {},
    suspend() {},
    resume() {},
    dispose: async () => {},
    runtime: {
      session: {
        sessionManager: { getBranch },
      },
    },
  } as unknown as WorkerHandle;
}
