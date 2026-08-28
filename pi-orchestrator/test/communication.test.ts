import assert from "node:assert/strict";
import test from "node:test";
import {
  captureDispatchReceipt,
  countUnreadResponses,
  dispatchToWorker,
  readWorkerMessages,
  readWorkerMessagesWithRecovery,
  waitForWorker,
  type DispatchSession,
  type ReadSession,
  type WaitSession,
} from "../src/communication.js";

test("dispatch receipt captures a stable pre-send cursor and ID", () => {
  const receipt = captureDispatchReceipt(
    { sessionManager: { getLeafId: () => "entry-before-send" } },
    () => "dispatch-123",
  );

  assert.deepEqual(receipt, {
    dispatchId: "dispatch-123",
    after: "entry-before-send",
  });
});

test("unread responses count completed replies rather than intermediate tool turns", () => {
  const session: ReadSession = {
    sessionManager: {
      getBranch: () => [
        { type: "message", id: "old", timestamp: "t0", message: { role: "assistant", content: "Old" } },
        { type: "message", id: "prompt", timestamp: "t1", message: { role: "user", content: "New work" } },
        { type: "message", id: "tool-turn-1", timestamp: "t2", message: { role: "assistant", stopReason: "toolUse", content: [{ type: "toolCall" }] } },
        { type: "message", id: "tool-result-1", timestamp: "t3", message: { role: "toolResult", content: "internal" } },
        { type: "message", id: "tool-turn-2", timestamp: "t4", message: { role: "assistant", stopReason: "toolUse", content: [{ type: "toolCall" }] } },
        { type: "message", id: "tool-result-2", timestamp: "t5", message: { role: "toolResult", content: "internal" } },
        { type: "message", id: "reply", timestamp: "t6", message: { role: "assistant", stopReason: "stop", content: "Done" } },
      ],
    },
  };

  assert.equal(countUnreadResponses(session, "old"), 1);
});

test("dispatch acknowledges an idle worker as soon as prompt preflight accepts", async () => {
  // Arrange
  let finishRun: (() => void) | undefined;
  const session: DispatchSession = {
    isStreaming: false,
    prompt: async (_text, options) => {
      options?.preflightResult?.(true);
      await new Promise<void>((resolve) => { finishRun = resolve; });
    },
    steer: async () => {},
    followUp: async () => {},
  };

  // Act
  const acknowledgement = await dispatchToWorker(session, "Inspect the tests.");

  // Assert
  assert.deepEqual(acknowledgement, { delivery: "immediate", accepted: true });
  assert.equal(typeof finishRun, "function", "the worker run should still be active after acknowledgement");
  finishRun?.();
});

test("dispatch queues a follow-up by default when the worker is busy", async () => {
  // Arrange
  const calls: string[] = [];
  const session: DispatchSession = {
    isStreaming: true,
    prompt: async () => { throw new Error("prompt should not be called"); },
    steer: async (text) => { calls.push(`steer:${text}`); },
    followUp: async (text) => { calls.push(`followUp:${text}`); },
  };

  // Act
  const acknowledgement = await dispatchToWorker(session, "Then summarize.");

  // Assert
  assert.deepEqual(calls, ["followUp:Then summarize."]);
  assert.deepEqual(acknowledgement, { delivery: "followUp", accepted: true });
});

test("dispatch can explicitly steer a busy worker", async () => {
  const calls: string[] = [];
  const session: DispatchSession = {
    isStreaming: true,
    prompt: async () => {},
    steer: async (text) => { calls.push(text); },
    followUp: async () => { throw new Error("followUp should not be called"); },
  };

  const acknowledgement = await dispatchToWorker(session, "Change direction.", "steer");

  assert.deepEqual(calls, ["Change direction."]);
  assert.equal(acknowledgement.delivery, "steer");
});

test("dispatch rejects an empty worker instruction", async () => {
  const session: DispatchSession = {
    isStreaming: false,
    prompt: async () => {},
    steer: async () => {},
    followUp: async () => {},
  };

  await assert.rejects(() => dispatchToWorker(session, "  "), /cannot be empty/);
});

test("wait resolves only after the worker agent becomes idle", async () => {
  // Arrange
  let release: (() => void) | undefined;
  let resolved = false;
  const session: WaitSession = {
    waitForIdle: async () => {
      await new Promise<void>((resolve) => { release = resolve; });
    },
  };

  // Act
  const waiting = waitForWorker(session).then(() => { resolved = true; });
  await Promise.resolve();

  // Assert
  assert.equal(resolved, false);
  release?.();
  await waiting;
  assert.equal(resolved, true);
});

test("read returns structured worker messages after a stable entry cursor", () => {
  // Arrange
  const session: ReadSession = {
    sessionManager: {
      getBranch: () => [
        { type: "message", id: "u1", timestamp: "2026-01-01T00:00:00Z", message: { role: "user", content: "Inspect." } },
        { type: "model_change", id: "m1", timestamp: "2026-01-01T00:00:01Z" },
        { type: "message", id: "a1", timestamp: "2026-01-01T00:00:02Z", message: { role: "assistant", content: [{ type: "text", text: "Done." }] } },
        { type: "message", id: "t1", timestamp: "2026-01-01T00:00:03Z", message: { role: "toolResult", toolName: "read", content: [{ type: "text", text: "file" }] } },
      ],
    },
  };

  // Act
  const result = readWorkerMessages(session, { after: "u1", limit: 10 });

  // Assert
  assert.deepEqual(result, {
    cursor: "t1",
    messages: [
      { id: "a1", timestamp: "2026-01-01T00:00:02Z", message: { role: "assistant", content: [{ type: "text", text: "Done." }] } },
      { id: "t1", timestamp: "2026-01-01T00:00:03Z", message: { role: "toolResult", toolName: "read", content: [{ type: "text", text: "file" }] } },
    ],
  });
});

test("read rejects a cursor that does not belong to the worker branch", () => {
  const session: ReadSession = {
    sessionManager: {
      getBranch: () => [
        { type: "message", id: "u1", timestamp: "2026-01-01T00:00:00Z", message: { role: "user", content: "Inspect." } },
      ],
    },
  };

  assert.throws(() => readWorkerMessages(session, { after: "other-worker-entry" }), /Unknown worker message cursor/);
});

test("read recovers from a stale cursor by reading from the start of the branch", () => {
  // Arrange: a native /new replaced the branch, so the old cursor no longer exists.
  const session: ReadSession = {
    sessionManager: {
      getBranch: () => [
        { type: "message", id: "fresh-u1", timestamp: "2026-01-02T00:00:00Z", message: { role: "user", content: "New task." } },
        { type: "message", id: "fresh-a1", timestamp: "2026-01-02T00:00:01Z", message: { role: "assistant", content: "Done." } },
      ],
    },
  };

  // Act
  const result = readWorkerMessagesWithRecovery(session, "stale-entry-id");

  // Assert
  assert.deepEqual(result, {
    cursor: "fresh-a1",
    messages: [
      { id: "fresh-u1", timestamp: "2026-01-02T00:00:00Z", message: { role: "user", content: "New task." } },
      { id: "fresh-a1", timestamp: "2026-01-02T00:00:01Z", message: { role: "assistant", content: "Done." } },
    ],
  });
});

test("read recovery on an empty branch returns no messages and a null cursor", () => {
  const session: ReadSession = {
    sessionManager: {
      getBranch: () => [],
    },
  };

  assert.deepEqual(readWorkerMessagesWithRecovery(session, "stale-entry-id"), {
    cursor: null,
    messages: [],
  });
});

test("read recovery does not mask an invalid limit", () => {
  const session: ReadSession = {
    sessionManager: {
      getBranch: () => [
        { type: "message", id: "fresh-u1", timestamp: "2026-01-02T00:00:00Z", message: { role: "user", content: "New task." } },
      ],
    },
  };

  assert.throws(
    () => readWorkerMessagesWithRecovery(session, "stale-entry-id", 101),
    /Read limit must be between 1 and 100/,
  );
});
