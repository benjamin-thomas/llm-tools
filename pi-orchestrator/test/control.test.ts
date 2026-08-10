import assert from "node:assert/strict";
import test from "node:test";
import { assertCoordinator } from "../src/host.js";

test("coordinator-only operations reject calls from workers", () => {
  assert.doesNotThrow(() => assertCoordinator("session-a", "session-a"));
  assert.throws(() => assertCoordinator("session-b", "session-a"), /only available to the orchestrator/);
});
