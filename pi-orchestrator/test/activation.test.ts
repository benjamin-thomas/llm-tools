import assert from "node:assert/strict";
import test from "node:test";
import {
  createActivationQueue,
  requestSerializedActivation,
  runSerializedActivation,
} from "../src/activation.js";

test("activation requests run serially without dropping targets", async () => {
  const queue = createActivationQueue();
  const calls: string[] = [];
  let releaseFirst: (() => void) | undefined;

  const activate = async (target: string) => {
    calls.push(`start:${target}`);
    if (target === "a") await new Promise<void>((resolve) => { releaseFirst = resolve; });
    calls.push(`end:${target}`);
  };

  const first = requestSerializedActivation(queue, "a", activate);
  await Promise.resolve();
  const second = requestSerializedActivation(queue, "b", activate);
  const third = requestSerializedActivation(queue, "c", activate);
  releaseFirst?.();
  await Promise.all([first, second, third]);

  assert.deepEqual(calls, [
    "start:a",
    "end:a",
    "start:b",
    "end:b",
    "start:c",
    "end:c",
  ]);
});

test("a failed activation does not prevent a queued teardown barrier", async () => {
  const queue = createActivationQueue();
  const calls: string[] = [];
  const failed = requestSerializedActivation(queue, "stopped", async () => {
    calls.push("activate");
    throw new Error("unavailable");
  });
  const teardown = runSerializedActivation(queue, async () => {
    calls.push("teardown");
  });

  await assert.rejects(failed, /unavailable/);
  await teardown;
  assert.deepEqual(calls, ["activate", "teardown"]);
});
