import assert from "node:assert/strict";
import test from "node:test";
import {
  formatStatus,
  formatTable,
  inWindow,
  parseWindow,
  prune,
  ratesForRoute,
  sampleFromAssistant,
  summarize,
  tokensPerSecond,
  weightedRate,
  type Sample,
} from "../src/stats.js";

const now = 1_000_000;

function sample(overrides: Partial<Sample> = {}): Sample {
  return {
    at: now,
    provider: "runinfra",
    model: "deepseek-v4-flash",
    output: 140,
    durationMs: 1000,
    ...overrides,
  };
}

test("sampleFromAssistant records end-to-end duration from message timestamp", () => {
  // Arrange: a finished assistant reply with provider usage
  // Act: convert it at a later wall clock
  const got = sampleFromAssistant(
    {
      role: "assistant",
      provider: "xai",
      model: "grok-4.6",
      timestamp: now - 2000,
      usage: { output: 120 },
      stopReason: "stop",
    },
    now,
  );

  // Assert: 120 tokens in 2s
  assert.deepEqual(got, {
    at: now,
    provider: "xai",
    model: "grok-4.6",
    output: 120,
    durationMs: 2000,
  });
  assert.equal(tokensPerSecond(got!), 60);
});

test("sampleFromAssistant skips failed, empty, and untimed replies", () => {
  const base = {
    role: "assistant" as const,
    provider: "xai",
    model: "grok-4.6",
    timestamp: now - 1000,
    usage: { output: 10 },
    stopReason: "stop",
  };

  assert.equal(sampleFromAssistant({ ...base, role: "user" }, now), undefined);
  assert.equal(sampleFromAssistant({ ...base, stopReason: "error" }, now), undefined);
  assert.equal(sampleFromAssistant({ ...base, stopReason: "aborted" }, now), undefined);
  assert.equal(sampleFromAssistant({ ...base, usage: { output: 0 } }, now), undefined);
  assert.equal(sampleFromAssistant({ ...base, timestamp: now }, now), undefined);
  assert.equal(sampleFromAssistant({ ...base, timestamp: now - 11 * 60 * 1000 }, now), undefined);
  const { provider: _provider, ...noProvider } = base;
  assert.equal(sampleFromAssistant(noProvider, now), undefined);
});

test("inWindow and prune keep only samples inside the retain period", () => {
  const samples = [
    sample({ at: now - 4 * 60 * 60 * 1000, output: 1 }),
    sample({ at: now - 30 * 60 * 1000, output: 2 }),
    sample({ at: now, output: 3 }),
  ];

  const hour = inWindow(samples, now, 60 * 60 * 1000);
  assert.deepEqual(hour.map((row) => row.output), [2, 3]);
  assert.deepEqual(prune(samples, now).map((row) => row.output), [2, 3]);
});

test("weightedRate is token-weighted so a short reply does not dominate", () => {
  // Arrange: a long fast generation, then a tiny slow one
  const samples = [
    sample({ output: 1000, durationMs: 10_000 }),
    sample({ output: 10, durationMs: 2000 }),
  ];

  // Act / Assert: 1010 tokens in 12s, not the unweighted mean of 100 and 5
  assert.equal(weightedRate(samples), 1010 / 12);
  assert.equal(weightedRate([]), undefined);
});

test("summarize reports token-weighted avg plus p50/p90, fastest first", () => {
  const samples = [
    sample({ provider: "slow", model: "a", output: 10, durationMs: 1000, at: now - 3 }),
    sample({ provider: "fast", model: "b", output: 100, durationMs: 1000, at: now - 2 }),
    sample({ provider: "fast", model: "b", output: 200, durationMs: 1000, at: now - 1 }),
    sample({ provider: "fast", model: "b", output: 300, durationMs: 1000, at: now }),
  ];

  const rows = summarize(samples);
  assert.equal(rows.length, 2);
  assert.equal(rows[0]!.route, "fast/b");
  assert.equal(rows[0]!.n, 3);
  assert.equal(rows[0]!.avg, 200);
  assert.equal(rows[0]!.p50, 200);
  assert.equal(rows[0]!.p90, 280);
  assert.equal(rows[1]!.route, "slow/a");
  assert.equal(rows[1]!.avg, 10);
});

test("parseWindow defaults to 10m and accepts 1m, 1h, and 3h", () => {
  assert.equal(parseWindow(""), "10m");
  assert.equal(parseWindow("1m"), "1m");
  assert.equal(parseWindow(" 1h "), "1h");
  assert.equal(parseWindow("3h"), "3h");
  assert.equal(parseWindow("10m"), "10m");
  assert.equal(parseWindow("2h"), undefined);
});

test("formatTable and formatStatus render the rolling window average", () => {
  const table = formatTable(
    [{ route: "xai/grok-4.6", n: 4, avg: 64.1, p50: 67.9, p90: 79.2 }],
    "10m",
  );
  assert.match(table, /Throughput last 10m/);
  assert.match(table, /xai\/grok-4.6/);
  assert.match(table, /64\.1/);
  assert.match(table, /67\.9/);

  const empty = formatTable([], "1h");
  assert.match(empty, /last 1h/);
  assert.match(empty, /No samples/);

  assert.equal(
    formatStatus([
      { window: "1m", avg: 70.4 },
      { window: "10m", avg: 68.2 },
      { window: "1h", avg: 58.4 },
      { window: "3h", avg: 55.1 },
    ]),
    "1m 70 · 10m 68 · 1h 58 · 3h 55 tok/s",
  );
  assert.equal(formatStatus([{ window: "3h", avg: 55.1 }]), "3h 55 tok/s");
  assert.equal(formatStatus([]), undefined);
});

test("ratesForRoute fills each window that has samples for that route", () => {
  const samples = [
    sample({ at: now - 2 * 60 * 60 * 1000, output: 55, durationMs: 1000 }),
    sample({ at: now - 20 * 60 * 1000, output: 58, durationMs: 1000 }),
    sample({ at: now - 90_000, output: 68, durationMs: 1000 }),
    sample({ at: now - 10_000, output: 80, durationMs: 1000 }),
  ];

  assert.deepEqual(ratesForRoute(samples, "runinfra/deepseek-v4-flash", now), [
    { window: "1m", avg: 80 },
    { window: "10m", avg: 74 },
    { window: "1h", avg: 68.66666666666667 },
    { window: "3h", avg: 65.25 },
  ]);
  assert.deepEqual(ratesForRoute(samples, "xai/grok-4.6", now), []);
});
