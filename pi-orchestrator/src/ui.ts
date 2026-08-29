import type { Theme } from "@earendil-works/pi-coding-agent";
import {
  matchesKey,
  truncateToWidth,
  visibleWidth,
  type Component,
  type KeyId,
} from "@earendil-works/pi-tui";
import { FIRST_WORKER_KEY, ORCHESTRATOR_ID, type OrchestratorSnapshot, type PublicWorker } from "./types.js";

export function preferredSwitcherIndex(
  snapshot: OrchestratorSnapshot,
  previousFocusedId?: string | null,
): number {
  if (snapshot.focusedId !== ORCHESTRATOR_ID) return 0;

  const workers = snapshot.workers.filter((worker) =>
    worker.activity === "idle" || worker.activity === "working",
  );
  const targetIds = [ORCHESTRATOR_ID, ...workers.map((worker) => worker.id)];
  const previousIndex = previousFocusedId ? targetIds.indexOf(previousFocusedId) : -1;
  if (previousIndex > 0) return previousIndex;
  return workers.length > 0 ? 1 : 0;
}

export function switchTargetForKey(
  data: string,
  snapshot: OrchestratorSnapshot,
): string | undefined {
  for (let digit = 0; digit <= 8; digit++) {
    if (!matchesKey(data, String(digit) as KeyId)) continue;
    if (digit === 0) return snapshot.active ? ORCHESTRATOR_ID : undefined;
    const slot = FIRST_WORKER_KEY + digit - 1;
    return snapshot.workers.find((worker) =>
      worker.slot === slot && (worker.activity === "idle" || worker.activity === "working"),
    )?.id;
  }
  return undefined;
}

function modelLabel(worker: PublicWorker): string {
  const model = worker.model.id.split("/").at(-1) ?? worker.model.id;
  const name = worker.name === model
    ? `${worker.name} (${worker.thinkingLevel})`
    : `${worker.name} (${model} · ${worker.thinkingLevel})`;
  return worker.unreadCount > 0 ? `${name} +${worker.unreadCount}` : name;
}

export class OrchestratorWidget implements Component {
  private frame = 0;
  private timer: NodeJS.Timeout | null = null;

  constructor(
    private readonly theme: Theme,
    private readonly getSnapshot: () => OrchestratorSnapshot,
    private readonly requestRender: () => void,
  ) {}

  render(width: number): string[] {
    const snapshot = this.getSnapshot();
    if (!snapshot.active || !snapshot.coordinator) {
      this.updateTimer(false);
      return [];
    }

    const roomAttention = snapshot.room?.pendingHumanRequests
      ? ` #human+${snapshot.room.pendingHumanRequests}`
      : snapshot.room?.moderationRequired
        ? " moderate!"
        : "";
    const coordinatorLabel = snapshot.mode === "room"
      ? `orchestrator [room]${roomAttention}`
      : "orchestrator";
    const entries = [
      this.segment(snapshot.coordinator.key, coordinatorLabel, snapshot.coordinator.activity, snapshot.coordinator.focused),
      ...snapshot.workers.map((worker) =>
        this.segment(worker.key, modelLabel(worker), worker.activity, worker.focused),
      ),
    ];
    this.updateTimer(entries.some((_, index) => {
      if (index === 0) return snapshot.coordinator?.activity === "working";
      return snapshot.workers[index - 1]?.activity === "working";
    }));

    const lines: string[] = [];
    let line = "";
    for (const entry of entries) {
      const candidate = line ? `${line}  ${this.theme.fg("dim", "│")}  ${entry}` : entry;
      if (line && visibleWidth(candidate) > width) {
        lines.push(truncateToWidth(line, width, "…"));
        line = truncateToWidth(entry, width, "…");
      } else {
        line = candidate;
      }
    }
    if (line) lines.push(truncateToWidth(line, width, "…"));
    return lines;
  }

  invalidate(): void {}

  dispose(): void {
    this.updateTimer(false);
  }

  private segment(key: string, name: string, activity: string, focused: boolean): string {
    const marker = activity === "working"
      ? this.theme.fg("accent", ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"][this.frame % 10]!)
      : activity === "error"
        ? this.theme.fg("error", "!")
        : this.theme.fg("success", "✓");
    const label = this.theme.fg(focused ? "accent" : "muted", `${key} ${name}`);
    return `${marker} ${label}`;
  }

  private updateTimer(run: boolean): void {
    if (run && !this.timer) {
      this.timer = setInterval(() => {
        this.frame++;
        this.requestRender();
      }, 80);
    } else if (!run && this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}
