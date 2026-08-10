import assert from "node:assert/strict";
import test from "node:test";
import { requestSerializedActivation, type ActivationQueue } from "../src/activation.js";

test("activation requests are serialized and the latest queued target wins", async () => {
  const queue: ActivationQueue = { inProgress: null, queuedTarget: null };
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

  assert.deepEqual(calls, ["start:a", "end:a", "start:c", "end:c"]);
});
