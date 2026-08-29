import assert from "node:assert/strict";
import test from "node:test";
import type { ExtensionContext } from "@earendil-works/pi-coding-agent";
import { createActivationQueue } from "../src/activation.js";
import {
  pumpRoomWorker,
  roomDeliveryPrompt,
  waitForRoomMessage,
  type SharedHost,
} from "../src/host.js";
import {
  createRoomState,
  postRoomMessage,
  recordRoomResponse,
  type RoomParticipant,
} from "../src/room.js";
import {
  ORCHESTRATOR_ID,
  SHARED_STATE_VERSION,
  type WorkerRecord,
} from "../src/types.js";

const participants: RoomParticipant[] = [
  { id: "worker-a", name: "a", activity: "idle" },
  { id: "worker-b", name: "b", activity: "idle" },
];
const sender = { id: ORCHESTRATOR_ID, name: "orchestrator", kind: "orchestrator" as const };

test("room delivery includes a distant call without advancing past omitted context", () => {
  const host = makeRoomHost();
  const room = host.room!;
  for (let index = 1; index <= 51; index++) {
    postRoomMessage(room, {
      sender,
      to: [],
      text: `${index}: ${"x".repeat(998)}`,
      workers: participants,
      id: () => `message-${index}`,
    });
  }

  const packet = roomDeliveryPrompt(room, host.workers[0]!.record, room.messages[50]!);

  assert.ok(packet.cursor < 50);
  assert.match(packet.text, /\[51\]/);
  assert.doesNotMatch(packet.text, /\[50\]/);
});

test("no-argument room wait tracks the open broadcast, not later calls", async () => {
  const host = makeRoomHost();
  const room = host.room!;
  postRoomMessage(room, {
    sender,
    to: ["all"],
    text: "Audit.",
    workers: participants,
    id: () => "broadcast-1",
  });
  recordRoomResponse(room, "broadcast-1", participants[0]!, "A response.");

  const waiting = waitForRoomMessage(host, coordinatorContext());
  postRoomMessage(room, {
    sender,
    to: ["a"],
    text: "Follow-up.",
    workers: participants,
    expectReply: true,
    id: () => "call-2",
  });
  recordRoomResponse(room, "broadcast-1", participants[1]!, "B response.");
  signalHost(host);

  assert.deepEqual(await waiting, {
    messageIds: ["broadcast-1"],
    settled: true,
    reason: "settled",
  });
  assert.equal(room.obligations.find((item) => item.messageId === "call-2")?.status, "pending");
});

test("room wait returns early when a moderation checkpoint pauses a broadcast", async () => {
  const host = makeRoomHost(1);
  const room = host.room!;
  postRoomMessage(room, {
    sender,
    to: ["all"],
    text: "Audit.",
    workers: participants,
    id: () => "broadcast-1",
  });

  const waiting = waitForRoomMessage(host, coordinatorContext());
  recordRoomResponse(room, "broadcast-1", participants[0]!, "A response.");
  signalHost(host);

  assert.deepEqual(await waiting, {
    messageIds: ["broadcast-1"],
    settled: false,
    reason: "moderation_required",
  });
  assert.equal(room.openBroadcastId, "broadcast-1");
});

test("room pumps leave pending obligations untouched during moderation", async () => {
  const host = makeRoomHost(1);
  const room = host.room!;
  postRoomMessage(room, {
    sender,
    to: ["all"],
    text: "Audit.",
    workers: [participants[0]!],
    id: () => "broadcast-1",
  });
  postRoomMessage(room, {
    sender: { id: "worker-a", name: "a", kind: "worker" },
    to: [],
    text: "Checkpoint.",
    workers: participants,
  });

  await pumpRoomWorker(host, "worker-a");

  assert.equal(room.obligations[0]?.status, "pending");
  assert.equal(host.roomPumps.size, 0);
});

test("a #human request interrupts a coordinator waiting on a broadcast", async () => {
  const host = makeRoomHost();
  const room = host.room!;
  postRoomMessage(room, {
    sender,
    to: ["all"],
    text: "Audit.",
    workers: participants,
    id: () => "broadcast-1",
  });
  const waiting = waitForRoomMessage(host, coordinatorContext());

  postRoomMessage(room, {
    sender: { id: "worker-a", name: "a", kind: "worker" },
    to: ["human"],
    text: "Need a decision.",
    workers: participants,
  });
  signalHost(host);

  assert.deepEqual(await waiting, {
    messageIds: ["broadcast-1"],
    settled: false,
    reason: "human_request",
  });
});

test("aborting room wait removes its host subscription", async () => {
  const host = makeRoomHost();
  postRoomMessage(host.room!, {
    sender,
    to: ["all"],
    text: "Audit.",
    workers: participants,
    id: () => "broadcast-1",
  });
  const controller = new AbortController();
  const waiting = waitForRoomMessage(host, coordinatorContext(), undefined, controller.signal);
  assert.equal(host.subscribers.size, 1);

  controller.abort(new Error("cancelled"));

  await assert.rejects(waiting, /cancelled/);
  assert.equal(host.subscribers.size, 0);
});

test("ending room orchestration rejects wait and removes its subscription", async () => {
  const host = makeRoomHost();
  postRoomMessage(host.room!, {
    sender,
    to: ["all"],
    text: "Audit.",
    workers: participants,
    id: () => "broadcast-1",
  });
  const waiting = waitForRoomMessage(host, coordinatorContext());

  host.active = false;
  host.room = null;
  signalHost(host);

  await assert.rejects(waiting, /ended while waiting/);
  assert.equal(host.subscribers.size, 0);
});

test("no-argument wait with unresolved #human returns human_request, not vacuously settled", async () => {
  const host = makeRoomHost();
  postRoomMessage(host.room!, {
    sender: { id: "worker-a", name: "a", kind: "worker" },
    to: ["human"],
    text: "Need help.",
    workers: participants,
  });

  const result = await waitForRoomMessage(host, coordinatorContext());
  assert.equal(result.reason, "human_request");
  assert.equal(result.settled, false);
});

function signalHost(host: SharedHost): void {
  for (const subscriber of [...host.subscribers]) subscriber();
}

function coordinatorContext(): ExtensionContext {
  return {
    sessionManager: {
      getSessionId: () => "coordinator-session",
      getSessionFile: () => undefined,
    },
  } as unknown as ExtensionContext;
}

function makeRoomHost(moderationEvery = 8): SharedHost {
  const workers: WorkerRecord[] = participants.map((participant, index) => ({
    id: participant.id,
    slot: 5 + index,
    name: participant.name,
    cwd: "/repo",
    model: { provider: "test", id: participant.name },
    thinkingLevel: "off",
    activity: participant.activity,
    sessionId: `${participant.id}-session`,
    createdAt: 1,
    lastActivityAt: 1,
    readCursor: null,
    unreadCount: 0,
  }));
  return {
    version: SHARED_STATE_VERSION,
    active: true,
    mode: "room",
    room: createRoomState(moderationEvery),
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
    workers: workers.map((record) => ({ record, handle: null })),
    subscribers: new Set(),
    activation: createActivationQueue(),
    parentTui: null,
    parentDone: null,
    parentHandoffActive: false,
    persist: null,
    lastPersistedState: null,
  };
}
