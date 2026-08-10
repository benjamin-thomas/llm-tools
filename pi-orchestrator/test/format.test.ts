import assert from "node:assert/strict";
import test from "node:test";
import { formatToolOutput } from "../src/format.js";

test("orchestrator tool output is formatted as readable YAML", () => {
  const output = formatToolOutput({
    cursor: "t1",
    messages: [{
      id: "a1",
      message: {
        role: "assistant",
        content: [{ type: "text", text: "First line\nSecond line" }],
      },
    }],
  });

  assert.match(output, /^cursor: t1\nmessages:\n/);
  assert.match(output, /  - id: a1/);
  assert.match(output, /role: assistant/);
  assert.doesNotMatch(output, /^\{/);
});

test("JSON objects embedded in nested strings are expanded into the YAML tree", () => {
  const output = formatToolOutput({
    message: {
      content: [{
        type: "text",
        text: '{"status":"ok","items":[1,2],"nested":"{\\"ready\\":true}"}',
      }],
    },
  });

  assert.match(output, /text:\n\s+status: ok/);
  assert.match(output, /items:\n\s+- 1\n\s+- 2/);
  assert.match(output, /nested:\n\s+ready: true/);
  assert.doesNotMatch(output, /\{\\?"status/);
});
