import assert from "node:assert/strict";
import test from "node:test";
import {
  HUMAN_ID,
  ORCHESTRATOR_ROOM_ID,
  configureRoomModeration,
  createRoomState,
  failRoomObligation,
  markRoomDelivered,
  moderateRoom,
  postRoomMessage,
  recordRoomResponse,
  resolveHumanRequest,
  roomMessageSettled,
  unreadRoomMessages,
  type RoomParticipant,
} from "../src/room.js";

const workers: RoomParticipant[] = [
  { id: "worker-john", name: "john", activity: "idle" },
  { id: "worker-jane", name: "jane", activity: "working" },
  { id: "worker-reviewer", name: "reviewer", activity: "idle" },
];

const orchestrator = { id: ORCHESTRATOR_ROOM_ID, name: "orchestrator", kind: "orchestrator" as const };
const john = { id: "worker-john", name: "john", kind: "worker" as const };

test("#all creates one response obligation for every live worker", () => {
  const state = createRoomState();
  const message = postRoomMessage(state, {
    sender: orchestrator,
    to: ["#all"],
    text: "Propose an approach.",
    workers,
    id: () => "broadcast-1",
    now: () => 10,
  });

  assert.equal(message.broadcast, true);
  assert.equal(state.openBroadcastId, "broadcast-1");
  assert.deepEqual(
    state.obligations.map(({ workerId, status }) => ({ workerId, status })),
    [
      { workerId: "worker-john", status: "pending" },
      { workerId: "worker-jane", status: "pending" },
      { workerId: "worker-reviewer", status: "pending" },
    ],
  );
});

test("a worker #all broadcast excludes its sender", () => {
  const state = createRoomState();
  postRoomMessage(state, {
    sender: john,
    to: ["all"],
    text: "Please challenge this.",
    workers,
    id: () => "broadcast-1",
  });

  assert.deepEqual(
    state.obligations.map((obligation) => obligation.workerId),
    ["worker-jane", "worker-reviewer"],
  );
});

test("a second broadcast is denied until the prior broadcast settles", () => {
  const state = createRoomState();
  postRoomMessage(state, {
    sender: orchestrator,
    to: ["all"],
    text: "First wave.",
    workers,
    id: () => "broadcast-1",
  });

  assert.throws(
    () => postRoomMessage(state, {
      sender: orchestrator,
      to: ["all"],
      text: "Second wave.",
      workers,
    }),
    /broadcast broadcast-1 is still open/,
  );

  for (const worker of workers) {
    markRoomDelivered(state, "broadcast-1", worker.id);
    recordRoomResponse(state, "broadcast-1", worker, `Reply from ${worker.name}`, {
      id: () => `reply-${worker.id}`,
    });
  }

  assert.equal(state.openBroadcastId, null);
  assert.equal(state.moderationRequired, true);
  assert.equal(roomMessageSettled(state, "broadcast-1"), true);
  assert.throws(() => postRoomMessage(state, {
    sender: orchestrator,
    to: ["all"],
    text: "Second wave.",
    workers,
  }), /moderation checkpoint/);
  moderateRoom(state, "continue");
  assert.doesNotThrow(() => postRoomMessage(state, {
    sender: orchestrator,
    to: ["all"],
    text: "Second wave.",
    workers,
  }));
});

test("prompt delivery alone does not close a broadcast", () => {
  const state = createRoomState();
  postRoomMessage(state, {
    sender: orchestrator,
    to: ["all"],
    text: "First wave.",
    workers,
    id: () => "broadcast-1",
  });

  for (const worker of workers) markRoomDelivered(state, "broadcast-1", worker.id);

  assert.equal(state.openBroadcastId, "broadcast-1");
  assert.equal(roomMessageSettled(state, "broadcast-1"), false);
});

test("named posts are visible tells by default", () => {
  const state = createRoomState();
  const message = postRoomMessage(state, {
    sender: orchestrator,
    to: ["#Jane", "reviewer"],
    text: "Review this.",
    workers,
    id: () => "message-1",
  });

  assert.deepEqual(message.recipients.map(({ id }) => id), ["worker-jane", "worker-reviewer"]);
  assert.deepEqual(state.obligations, []);
});

test("a tell with replyTo does not settle the original response obligation", () => {
  const state = createRoomState();
  postRoomMessage(state, {
    sender: orchestrator,
    to: ["john"],
    text: "Audit.",
    workers,
    expectReply: true,
    id: () => "call-1",
  });
  postRoomMessage(state, {
    sender: john,
    to: ["orchestrator"],
    text: "Working on it.",
    workers,
    replyTo: "call-1",
    id: () => "tell-1",
  });

  assert.equal(state.obligations[0]?.status, "pending");
  assert.equal(roomMessageSettled(state, "call-1"), false);
});

test("named posts create obligations only when a reply is explicitly requested", () => {
  const state = createRoomState();
  postRoomMessage(state, {
    sender: orchestrator,
    to: ["#Jane", "reviewer"],
    text: "Review this.",
    workers,
    expectReply: true,
    id: () => "message-1",
  });

  assert.deepEqual(state.obligations.map(({ workerId }) => workerId), ["worker-jane", "worker-reviewer"]);
});

test("#human creates a resolvable human-attention request without a worker obligation", () => {
  const state = createRoomState();
  const message = postRoomMessage(state, {
    sender: john,
    to: ["#human"],
    text: "Which behavior is expected?",
    workers,
    id: () => "human-1",
  });

  assert.deepEqual(message.recipients, [{ id: HUMAN_ID, name: "human" }]);
  assert.equal(message.humanRequest, true);
  assert.equal(state.obligations.length, 0);
  assert.equal(resolveHumanRequest(state, "human-1"), true);
  assert.equal(message.resolved, true);
});

test("failed recipients settle a broadcast visibly instead of deadlocking it", () => {
  const state = createRoomState();
  postRoomMessage(state, {
    sender: orchestrator,
    to: ["all"],
    text: "First wave.",
    workers: [workers[0]!],
    id: () => "broadcast-1",
  });

  failRoomObligation(state, "broadcast-1", "worker-john", "worker stopped");

  assert.equal(roomMessageSettled(state, "broadcast-1"), true);
  assert.equal(state.openBroadcastId, null);
  assert.equal(state.moderationRequired, true);
  assert.equal(state.obligations[0]?.error, "worker stopped");
});

test("room cursors advance independently for each participant", () => {
  const state = createRoomState();
  postRoomMessage(state, {
    sender: orchestrator,
    to: [],
    text: "Shared context.",
    workers,
    id: () => "message-1",
  });

  assert.equal(unreadRoomMessages(state, "worker-john").messages.length, 1);
  assert.equal(unreadRoomMessages(state, "worker-john").messages.length, 0);
  assert.equal(unreadRoomMessages(state, "worker-jane").messages.length, 1);
});

test("the orchestrator can configure the moderation response interval", () => {
  const state = createRoomState();
  configureRoomModeration(state, 2);
  assert.equal(state.moderationEvery, 2);
  assert.throws(() => configureRoomModeration(state, 0), /between 1 and 100/);
});

test("moderation checkpoints block worker fan-out until the orchestrator continues", () => {
  const state = createRoomState(1);
  postRoomMessage(state, {
    sender: john,
    to: [],
    text: "An update.",
    workers,
    id: () => "message-1",
  });

  assert.equal(state.moderationRequired, true);
  assert.throws(
    () => postRoomMessage(state, {
      sender: john,
      to: ["jane"],
      text: "Please respond.",
      workers,
      expectReply: true,
    }),
    /moderation checkpoint/,
  );

  moderateRoom(state, "continue");
  assert.equal(state.moderationRequired, false);
  assert.doesNotThrow(() => postRoomMessage(state, {
    sender: john,
    to: ["jane"],
    text: "Please respond.",
    workers,
    expectReply: true,
  }));
});

test("worker tells do not extend an open broadcast", () => {
  const state = createRoomState();
  postRoomMessage(state, {
    sender: orchestrator,
    to: ["all"],
    text: "Audit independently.",
    workers,
    id: () => "broadcast-1",
  });

  postRoomMessage(state, {
    sender: john,
    to: ["jane"],
    text: "I found a cursor issue.",
    workers,
    id: () => "tell-1",
  });

  assert.equal(state.obligations.length, workers.length);
  assert.throws(() => postRoomMessage(state, {
    sender: john,
    to: ["jane"],
    text: "Please confirm.",
    workers,
    expectReply: true,
  }), /cannot create response obligations/);
});

test("a worker can owe at most one outstanding room response", () => {
  const state = createRoomState();
  postRoomMessage(state, {
    sender: orchestrator,
    to: ["jane"],
    text: "First call.",
    workers,
    expectReply: true,
    id: () => "call-1",
  });

  assert.throws(() => postRoomMessage(state, {
    sender: orchestrator,
    to: ["jane"],
    text: "Second call.",
    workers,
    expectReply: true,
  }), /already owes a response.*call-1/);
});

test("moderation can continue an open broadcast checkpoint", () => {
  const state = createRoomState(1);
  postRoomMessage(state, {
    sender: orchestrator,
    to: ["all"],
    text: "Audit independently.",
    workers,
    id: () => "broadcast-1",
  });
  postRoomMessage(state, {
    sender: john,
    to: [],
    text: "Progress update.",
    workers,
  });

  assert.equal(state.moderationRequired, true);
  assert.equal(state.openBroadcastId, "broadcast-1");
  assert.doesNotThrow(() => moderateRoom(state, "continue"));
  assert.equal(state.moderationRequired, false);
  assert.throws(() => moderateRoom(state, "conclude"), /Cannot conclude while broadcast/);
});

test("explicit room reads never rewind or skip the stored cursor", () => {
  const state = createRoomState();
  for (let index = 1; index <= 10; index++) {
    postRoomMessage(state, {
      sender: orchestrator,
      to: [],
      text: `Message ${index}`,
      workers,
      id: () => `message-${index}`,
    });
  }
  state.cursors["worker-john"] = 10;

  assert.equal(unreadRoomMessages(state, "worker-john", { after: 0, limit: 5 }).cursor, 5);
  assert.equal(state.cursors["worker-john"], 10);
  assert.equal(unreadRoomMessages(state, "worker-john", { after: 100 }).cursor, 100);
  assert.equal(state.cursors["worker-john"], 10);
  unreadRoomMessages(state, "worker-john", { after: 0, limit: 5, advance: true });
  assert.equal(state.cursors["worker-john"], 10);
});

test("a checkpoint blocks worker chatter but still permits #human requests", () => {
  const state = createRoomState(1);
  postRoomMessage(state, {
    sender: john,
    to: [],
    text: "First update.",
    workers,
  });

  assert.throws(() => postRoomMessage(state, {
    sender: john,
    to: [],
    text: "More chatter.",
    workers,
  }), /posting is paused/);
  assert.doesNotThrow(() => postRoomMessage(state, {
    sender: john,
    to: ["human"],
    text: "Need a decision.",
    workers,
  }));
  assert.equal(state.messagesSinceModeration, 1);
});

test("creating workers are not eligible room recipients", () => {
  const state = createRoomState();
  const creating = { id: "worker-new", name: "new", activity: "creating" as const };
  postRoomMessage(state, {
    sender: orchestrator,
    to: ["all"],
    text: "Audit.",
    workers: [...workers, creating],
  });

  assert.equal(state.obligations.some((obligation) => obligation.workerId === creating.id), false);
  assert.throws(() => postRoomMessage(createRoomState(), {
    sender: orchestrator,
    to: ["new"],
    text: "Audit.",
    workers: [...workers, creating],
  }), /recipient is unavailable/);
});

test("a failed obligation cannot be resurrected by a late response", () => {
  const state = createRoomState();
  postRoomMessage(state, {
    sender: orchestrator,
    to: ["jane"],
    text: "Respond.",
    workers,
    expectReply: true,
    id: () => "call-1",
  });
  failRoomObligation(state, "call-1", "worker-jane", "stopped");

  assert.throws(
    () => recordRoomResponse(state, "call-1", workers[1]!, "Late response."),
    /obligation already failed/,
  );
  assert.equal(state.obligations[0]?.status, "failed");
  assert.equal(state.messages.length, 1);
});

test("addressing #orchestrator requests an immediate moderation checkpoint", () => {
  const state = createRoomState();
  postRoomMessage(state, {
    sender: john,
    to: ["orchestrator"],
    text: "Please redirect the discussion.",
    workers,
  });

  assert.equal(state.moderationRequired, true);
});

test("moderation is rejected after the room has concluded", () => {
  const state = createRoomState();
  moderateRoom(state, "conclude");
  assert.equal(state.concluded, true);
  assert.throws(() => moderateRoom(state, "continue"), /already concluded/);
  assert.throws(() => moderateRoom(state, "conclude"), /already concluded/);
});
