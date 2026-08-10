import assert from "node:assert/strict";
import test from "node:test";
import { workerToolNames } from "../src/tool-policy.js";

test("worker tool policy removes the coordinator-only orchestrator tool", () => {
  assert.deepEqual(
    workerToolNames(["read", "orchestrator", "bash"]),
    ["read", "bash"],
  );
});
