export type WindowKey = "1m" | "10m" | "1h" | "3h";

export type Sample = {
  at: number;
  provider: string;
  model: string;
  output: number;
  durationMs: number;
};

export type AssistantLike = {
  role: string;
  provider?: string;
  model?: string;
  timestamp?: number;
  stopReason?: string;
  usage?: { output?: number };
};

export type RouteStats = {
  route: string;
  n: number;
  avg: number;
  p50: number;
  p90: number;
};

export const WINDOWS = {
  "1m": 60 * 1000,
  "10m": 10 * 60 * 1000,
  "1h": 60 * 60 * 1000,
  "3h": 3 * 60 * 60 * 1000,
} as const;

export const WINDOW_KEYS: WindowKey[] = ["1m", "10m", "1h", "3h"];
export const DEFAULT_WINDOW: WindowKey = "10m";
export const RETAIN_MS = WINDOWS["3h"];
export const MAX_DURATION_MS = 10 * 60 * 1000;
export const SAMPLE_ENTRY_TYPE = "tps-sample";

const SKIP_STOP = new Set(["error", "aborted", "pending"]);

export function parseWindow(arg: string): WindowKey | undefined {
  const key = arg.trim();
  if (key === "") return DEFAULT_WINDOW;
  if (key === "1m" || key === "10m" || key === "1h" || key === "3h") return key;
  return undefined;
}

export function tokensPerSecond(sample: Sample): number {
  return sample.output / (sample.durationMs / 1000);
}

/** Token-weighted tok/s so a short reply cannot dominate. Idle time is not sampled. */
export function weightedRate(samples: readonly Sample[]): number | undefined {
  let output = 0;
  let durationMs = 0;
  for (const sample of samples) {
    output += sample.output;
    durationMs += sample.durationMs;
  }
  if (durationMs <= 0 || output <= 0) return undefined;
  return output / (durationMs / 1000);
}

export function routeOf(sample: Sample): string {
  return `${sample.provider}/${sample.model}`;
}

export function sampleFromAssistant(message: AssistantLike, now: number): Sample | undefined {
  if (message.role !== "assistant") return undefined;
  if (message.stopReason !== undefined && SKIP_STOP.has(message.stopReason)) return undefined;
  const provider = message.provider;
  const model = message.model;
  const start = message.timestamp;
  const output = message.usage?.output;
  if (!provider || !model) return undefined;
  if (typeof start !== "number" || !Number.isFinite(start)) return undefined;
  if (typeof output !== "number" || !Number.isFinite(output) || output <= 0) return undefined;
  const durationMs = now - start;
  if (durationMs <= 0 || durationMs > MAX_DURATION_MS) return undefined;
  return { at: now, provider, model, output, durationMs };
}

export function inWindow(samples: readonly Sample[], now: number, windowMs: number): Sample[] {
  const start = now - windowMs;
  return samples.filter((sample) => sample.at >= start && sample.at <= now);
}

export function prune(samples: readonly Sample[], now: number, retainMs: number = RETAIN_MS): Sample[] {
  return inWindow(samples, now, retainMs);
}

export function isSample(value: unknown): value is Sample {
  if (!value || typeof value !== "object") return false;
  const row = value as Record<string, unknown>;
  return (
    typeof row.at === "number" &&
    Number.isFinite(row.at) &&
    typeof row.provider === "string" &&
    row.provider.length > 0 &&
    typeof row.model === "string" &&
    row.model.length > 0 &&
    typeof row.output === "number" &&
    Number.isFinite(row.output) &&
    typeof row.durationMs === "number" &&
    Number.isFinite(row.durationMs)
  );
}

export function samplesFromEntries(entries: readonly { type: string; customType?: string; data?: unknown }[]): Sample[] {
  const samples: Sample[] = [];
  for (const entry of entries) {
    if (entry.type !== "custom" || entry.customType !== SAMPLE_ENTRY_TYPE) continue;
    if (isSample(entry.data)) samples.push(entry.data);
  }
  return samples;
}

function percentile(sorted: readonly number[], p: number): number {
  if (sorted.length === 0) throw new Error("percentile of empty list");
  if (sorted.length === 1) return sorted[0]!;
  const i = (p / 100) * (sorted.length - 1);
  const lo = Math.floor(i);
  const hi = Math.ceil(i);
  const a = sorted[lo]!;
  const b = sorted[hi]!;
  return a + (b - a) * (i - lo);
}

export function summarize(samples: readonly Sample[]): RouteStats[] {
  const groups = new Map<string, Sample[]>();
  for (const sample of samples) {
    const route = routeOf(sample);
    const group = groups.get(route);
    if (group) group.push(sample);
    else groups.set(route, [sample]);
  }

  const rows: RouteStats[] = [];
  for (const [route, group] of groups) {
    const avg = weightedRate(group);
    if (avg === undefined) continue;
    const rates = group.map(tokensPerSecond).sort((a, b) => a - b);
    rows.push({
      route,
      n: group.length,
      avg,
      p50: percentile(rates, 50),
      p90: percentile(rates, 90),
    });
  }
  rows.sort((a, b) => b.avg - a.avg || a.route.localeCompare(b.route));
  return rows;
}

export function formatRate(tps: number): string {
  const clamped = Math.max(0, tps);
  if (clamped >= 10) return String(Math.round(clamped));
  return String(Math.round(clamped * 10) / 10);
}

function formatTableRate(tps: number): string {
  return tps.toFixed(1);
}

export function formatTable(stats: readonly RouteStats[], windowKey: WindowKey): string {
  const header = `Throughput last ${windowKey} (generation wall time, TTFT included)`;
  if (stats.length === 0) {
    return `${header}\n\nNo samples in this window.`;
  }

  const routeWidth = Math.max(5, ...stats.map((row) => row.route.length));
  const lines = [
    header,
    "",
    `${"route".padEnd(routeWidth)}  ${"n".padStart(4)}  ${"avg".padStart(7)}  ${"p50".padStart(7)}  ${"p90".padStart(7)}`,
  ];
  for (const row of stats) {
    lines.push(
      `${row.route.padEnd(routeWidth)}  ${String(row.n).padStart(4)}  ${formatTableRate(row.avg).padStart(7)}  ${formatTableRate(row.p50).padStart(7)}  ${formatTableRate(row.p90).padStart(7)}`,
    );
  }
  return lines.join("\n");
}

export function ratesForRoute(
  samples: readonly Sample[],
  route: string,
  now: number,
): { window: WindowKey; avg: number }[] {
  const parts: { window: WindowKey; avg: number }[] = [];
  for (const window of WINDOW_KEYS) {
    const avg = weightedRate(inWindow(samples, now, WINDOWS[window]).filter((sample) => routeOf(sample) === route));
    if (avg !== undefined) parts.push({ window, avg });
  }
  return parts;
}

export function formatStatus(parts: readonly { window: WindowKey; avg: number }[]): string | undefined {
  if (parts.length === 0) return undefined;
  return `${parts.map((part) => `${part.window} ${formatRate(part.avg)}`).join(" · ")} tok/s`;
}
