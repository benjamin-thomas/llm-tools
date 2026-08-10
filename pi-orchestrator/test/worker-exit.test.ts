import assert from "node:assert/strict";
import test from "node:test";
import { shouldCloseWorker } from "../src/worker-exit.js";

test("Ctrl+D closes only a worker with an empty editor", () => {
  assert.equal(shouldCloseWorker("\x04", "", true), true);
  assert.equal(shouldCloseWorker("\x04", "text", true), false);
  assert.equal(shouldCloseWorker("\x04", "", false), false);
  assert.equal(shouldCloseWorker("x", "", true), false);
});
