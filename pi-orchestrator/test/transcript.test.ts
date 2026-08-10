import assert from "node:assert/strict";
import test from "node:test";
import { transcriptMessages } from "../src/transcript.js";

test("worker transcript labels prompt sender and response receiver clearly", () => {
  const messages = transcriptMessages("kimi-k3", [
    { id: "u1", timestamp: "t1", message: { role: "user", content: "Inspect the tests." } },
    { id: "tool", timestamp: "t2", message: { role: "toolResult", toolName: "read", content: [{ type: "text", text: "internal output" }] } },
    { id: "a1", timestamp: "t3", message: { role: "assistant", content: [{ type: "thinking", thinking: "hidden" }, { type: "text", text: "The tests look sound." }] } },
  ]);

  assert.deepEqual(messages, [
    { sender: "orchestrator", receiver: "kimi-k3", text: "Inspect the tests." },
    { sender: "kimi-k3", receiver: "orchestrator", text: "The tests look sound." },
  ]);
});
