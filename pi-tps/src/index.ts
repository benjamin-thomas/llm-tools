import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
  formatStatus,
  formatTable,
  inWindow,
  parseWindow,
  ratesForRoute,
  routeOf,
  sampleFromAssistant,
  SAMPLE_ENTRY_TYPE,
  samplesFromEntries,
  summarize,
  WINDOWS,
  type Sample,
  type WindowKey,
} from "./stats.js";

const STATUS_KEY = "tps";

export default function tpsExtension(pi: ExtensionAPI): void {
  let samples: Sample[] = [];

  const refreshStatus = (ctx: { hasUI: boolean; ui: { setStatus: (key: string, text: string | undefined) => void } }, now: number) => {
    if (!ctx.hasUI) return;
    const last = newest(samples);
    if (!last) {
      ctx.ui.setStatus(STATUS_KEY, undefined);
      return;
    }
    ctx.ui.setStatus(STATUS_KEY, formatStatus(ratesForRoute(samples, routeOf(last), now)));
  };

  pi.on("session_start", async (_event, ctx) => {
    const now = Date.now();
    samples = samplesFromEntries(ctx.sessionManager.getEntries());
    refreshStatus(ctx, now);
  });

  pi.on("message_end", async (event, ctx) => {
    const now = Date.now();
    const sample = sampleFromAssistant(event.message, now);
    if (!sample) return;
    samples = [...samples, sample];
    pi.appendEntry(SAMPLE_ENTRY_TYPE, sample);
    refreshStatus(ctx, now);
  });

  pi.registerCommand("tps", {
    description: "Show token throughput by provider/model (1m, 10m, 1h, or 3h)",
    getArgumentCompletions: (prefix) => {
      const options = ["1m", "10m", "1h", "3h"];
      const filtered = options.filter((option) => option.startsWith(prefix.trim()));
      return filtered.length > 0 ? filtered.map((value) => ({ value, label: value })) : null;
    },
    handler: async (args, ctx) => {
      const windowKey: WindowKey | undefined = parseWindow(args);
      if (!windowKey) {
        ctx.ui.notify("Usage: /tps [1m|10m|1h|3h]", "error");
        return;
      }
      const now = Date.now();
      const windowed = inWindow(samples, now, WINDOWS[windowKey]);
      ctx.ui.notify(formatTable(summarize(windowed), windowKey), "info");
    },
  });
}

function newest(samples: readonly Sample[]): Sample | undefined {
  if (samples.length === 0) return undefined;
  return samples.reduce((a, b) => (a.at >= b.at ? a : b));
}
