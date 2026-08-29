import type { ThinkingLevel } from "@earendil-works/pi-agent-core";

export const ORCHESTRATOR_ID = "__orchestrator__";
export const SHARED_STATE_VERSION = 4 as const;
export const FIRST_WORKER_KEY = 5;
export const LAST_WORKER_KEY = 12;

export type Activity = "creating" | "idle" | "working" | "stopped" | "error";
export type OrchestrationMode = "silo" | "room";

export interface ScopedModelSpec {
  provider: string;
  id: string;
  thinkingLevel?: ThinkingLevel;
}

export interface WorkerRecord {
  id: string;
  slot: number;
  name: string;
  cwd: string;
  model: ScopedModelSpec;
  thinkingLevel: ThinkingLevel;
  activity: Activity;
  sessionId?: string;
  sessionFile?: string;
  createdAt: number;
  lastActivityAt: number;
  readCursor: string | null;
  unreadCount: number;
  error?: string;
}

export interface CoordinatorRecord {
  id: typeof ORCHESTRATOR_ID;
  name: "orchestrator";
  cwd: string;
  model?: ScopedModelSpec;
  thinkingLevel: ThinkingLevel;
  activity: "idle" | "working";
  sessionId: string;
  sessionFile?: string;
  lastActivityAt: number;
}

export interface PublicWorker extends WorkerRecord {
  key: string;
  focused: boolean;
}

export interface RoomSnapshot {
  messages: number;
  openBroadcastId: string | null;
  moderationRequired: boolean;
  pendingHumanRequests: number;
}

export interface OrchestratorSnapshot {
  active: boolean;
  mode: OrchestrationMode;
  focusedId: string;
  room?: RoomSnapshot;
  coordinator?: CoordinatorRecord & { focused: boolean; key: "0" };
  workers: PublicWorker[];
  scopedModels: ScopedModelSpec[];
}

export interface SpawnRequest {
  count?: number;
  models?: string[];
}
