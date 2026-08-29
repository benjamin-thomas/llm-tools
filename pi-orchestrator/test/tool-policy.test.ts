import assert from "node:assert/strict";
import test from "node:test";
import { workerToolNames } from "../src/tool-policy.js";

test("worker tool policy removes coordinator and inactive room controls", () => {
  assert.deepEqual(
    workerToolNames(["read", "orchestrator", "room", "bash"]),
    ["read", "bash"],
  );
  assert.deepEqual(
    workerToolNames(["read", "orchestrator", "room", "bash"], true),
    ["read", "room", "bash"],
  );
});
