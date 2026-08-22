import assert from "node:assert/strict";
import test from "node:test";
import { SAMPLE_ENTRY_TYPE, samplesFromEntries } from "../src/stats.js";

test("samplesFromEntries keeps only tps-sample custom entries from this session", () => {
  const samples = samplesFromEntries([
    { type: "message" },
    { type: "custom", customType: "other", data: { at: 1, provider: "xai", model: "grok-4.6", output: 1, durationMs: 1 } },
    { type: "custom", customType: SAMPLE_ENTRY_TYPE, data: { at: 10, provider: "xai", model: "grok-4.6", output: 20, durationMs: 1000 } },
    { type: "custom", customType: SAMPLE_ENTRY_TYPE, data: "nope" },
    { type: "custom", customType: SAMPLE_ENTRY_TYPE, data: { at: 11, provider: "runinfra", model: "qwen3-8-27b", output: 30, durationMs: 500 } },
  ]);

  assert.deepEqual(samples, [
    { at: 10, provider: "xai", model: "grok-4.6", output: 20, durationMs: 1000 },
    { at: 11, provider: "runinfra", model: "qwen3-8-27b", output: 30, durationMs: 500 },
  ]);
});
