import { randomUUID } from "node:crypto";
import { ORCHESTRATOR_ID, type Activity } from "./types.js";

export const HUMAN_ID = "__human__";
export const ORCHESTRATOR_ROOM_ID = ORCHESTRATOR_ID;
export const DEFAULT_MODERATION_EVERY = 8;
const MAX_ROOM_MESSAGE_CHARS = 50_000;

export type RoomSenderKind = "orchestrator" | "worker";
export type RoomObligationStatus = "pending" | "delivered" | "responded" | "failed";

export interface RoomParticipant {
  id: string;
  name: string;
  activity: Activity;
}

export interface RoomAddress {
  id: string;
  name: string;
}

export interface RoomMessage {
  id: string;
  sequence: number;
  timestamp: number;
  sender: RoomAddress;
  recipients: RoomAddress[];
  text: string;
  broadcast: boolean;
  humanRequest: boolean;
  resolved?: boolean;
  replyTo?: string;
}

export interface RoomObligation {
  messageId: string;
  workerId: string;
  status: RoomObligationStatus;
  responseMessageId?: string;
  error?: string;
}

export interface RoomState {
  nextSequence: number;
  messages: RoomMessage[];
  obligations: RoomObligation[];
  cursors: Record<string, number>;
  openBroadcastId: string | null;
  moderationEvery: number;
  messagesSinceModeration: number;
  moderationRequired: boolean;
  concluded: boolean;
}

export interface RoomSender extends RoomAddress {
  kind: RoomSenderKind;
}

interface PostRoomOptions {
  sender: RoomSender;
  to: readonly string[];
  text: string;
  workers: readonly RoomParticipant[];
  expectReply?: boolean;
  replyTo?: string;
  id?: () => string;
  now?: () => number;
}

export function createRoomState(moderationEvery = DEFAULT_MODERATION_EVERY): RoomState {
  if (!Number.isInteger(moderationEvery) || moderationEvery < 1) {
    throw new Error("Room moderation interval must be a positive integer.");
  }
  return {
    nextSequence: 1,
    messages: [],
    obligations: [],
    cursors: {},
    openBroadcastId: null,
    moderationEvery,
    messagesSinceModeration: 0,
    moderationRequired: false,
    concluded: false,
  };
}

function normalizedAddress(value: string): string {
  return value.trim().replace(/^#+/, "").toLowerCase();
}

function liveWorkers(workers: readonly RoomParticipant[]): RoomParticipant[] {
  return workers.filter((worker) => worker.activity === "idle" || worker.activity === "working");
}

function recipientAddresses(
  names: readonly string[],
  sender: RoomSender,
  workers: readonly RoomParticipant[],
): { recipients: RoomAddress[]; broadcast: boolean } {
  const normalized = names.map(normalizedAddress).filter(Boolean);
  const wantsAll = normalized.includes("all");
  if (wantsAll && normalized.length !== 1) {
    throw new Error("#all cannot be combined with other room recipients.");
  }

  if (wantsAll) {
    return {
      recipients: liveWorkers(workers)
        .filter((worker) => sender.kind !== "worker" || worker.id !== sender.id)
        .map(({ id, name }) => ({ id, name })),
      broadcast: true,
    };
  }

  const byName = new Map(workers.map((worker) => [worker.name.toLowerCase(), worker]));
  const recipients: RoomAddress[] = [];
  const seen = new Set<string>();
  for (const name of normalized) {
    let recipient: RoomAddress | undefined;
    if (name === "human") recipient = { id: HUMAN_ID, name: "human" };
    else if (name === "orchestrator") {
      recipient = { id: ORCHESTRATOR_ROOM_ID, name: "orchestrator" };
    } else {
      const worker = byName.get(name);
      if (!worker) throw new Error(`Unknown room recipient: #${name}`);
      if (worker.activity !== "idle" && worker.activity !== "working") {
        throw new Error(`Room recipient is unavailable: #${worker.name}`);
      }
      if (sender.kind === "worker" && worker.id === sender.id) {
        throw new Error("A worker cannot call itself in the room.");
      }
      recipient = { id: worker.id, name: worker.name };
    }
    if (!seen.has(recipient.id)) {
      recipients.push(recipient);
      seen.add(recipient.id);
    }
  }
  return { recipients, broadcast: false };
}

function boundedText(text: string): string {
  if (text.length <= MAX_ROOM_MESSAGE_CHARS) return text;
  const suffix = `\n\n[Room message truncated at ${MAX_ROOM_MESSAGE_CHARS} characters.]`;
  return `${text.slice(0, MAX_ROOM_MESSAGE_CHARS - suffix.length)}${suffix}`;
}

function appendMessage(state: RoomState, message: Omit<RoomMessage, "sequence">): RoomMessage {
  const complete = { ...message, sequence: state.nextSequence++ };
  state.messages.push(complete);
  return complete;
}

function countWorkerMessage(state: RoomState): void {
  if (state.moderationRequired) return;
  state.messagesSinceModeration++;
  if (state.messagesSinceModeration >= state.moderationEvery) {
    state.moderationRequired = true;
  }
}

export function postRoomMessage(state: RoomState, options: PostRoomOptions): RoomMessage {
  const text = options.text.trim();
  if (!text) throw new Error("Room message cannot be empty.");
  if (options.replyTo && !state.messages.some((message) => message.id === options.replyTo)) {
    throw new Error(`Unknown room message to reply to: ${options.replyTo}`);
  }

  const addressed = recipientAddresses(options.to, options.sender, options.workers);
  const workerRecipients = addressed.recipients.filter((recipient) =>
    recipient.id !== HUMAN_ID && recipient.id !== ORCHESTRATOR_ROOM_ID,
  );
  const expectsWorkerReply = addressed.broadcast || options.expectReply === true;
  const humanOnly = addressed.recipients.length > 0
    && addressed.recipients.every((recipient) => recipient.id === HUMAN_ID);

  if (addressed.broadcast && options.expectReply === false) {
    throw new Error("#all always requires every eligible worker to respond.");
  }
  if (addressed.broadcast && state.openBroadcastId) {
    throw new Error(
      `Room broadcast ${state.openBroadcastId} is still open; every recipient must respond before another #all.`,
    );
  }
  if (addressed.broadcast && workerRecipients.length === 0) {
    throw new Error("#all has no eligible worker recipients.");
  }
  if (options.expectReply === true && workerRecipients.length === 0) {
    throw new Error("A response call requires at least one worker recipient.");
  }
  if (state.moderationRequired && expectsWorkerReply) {
    throw new Error("The orchestrator must complete the moderation checkpoint before another response call.");
  }
  if (options.sender.kind === "worker" && state.moderationRequired && !humanOnly) {
    throw new Error("Room posting is paused for an orchestrator moderation checkpoint.");
  }
  if (options.sender.kind === "worker" && state.openBroadcastId && expectsWorkerReply) {
    throw new Error("Workers cannot create response obligations while a #all broadcast is open.");
  }
  if (state.concluded && (expectsWorkerReply || (options.sender.kind === "worker" && !humanOnly))) {
    throw new Error("Room deliberation has concluded.");
  }
  if (expectsWorkerReply) {
    for (const recipient of workerRecipients) {
      const outstanding = state.obligations.find((obligation) =>
        obligation.workerId === recipient.id
        && (obligation.status === "pending" || obligation.status === "delivered"),
      );
      if (outstanding) {
        throw new Error(
          `#${recipient.name} already owes a response to room message ${outstanding.messageId}.`,
        );
      }
    }
  }

  const message = appendMessage(state, {
    id: (options.id ?? randomUUID)(),
    timestamp: (options.now ?? Date.now)(),
    sender: { id: options.sender.id, name: options.sender.name },
    recipients: addressed.recipients,
    text: boundedText(text),
    broadcast: addressed.broadcast,
    humanRequest: addressed.recipients.some((recipient) => recipient.id === HUMAN_ID),
    ...(options.replyTo ? { replyTo: options.replyTo } : {}),
  });

  if (expectsWorkerReply) {
    for (const recipient of workerRecipients) {
      state.obligations.push({ messageId: message.id, workerId: recipient.id, status: "pending" });
    }
  }
  if (message.broadcast) state.openBroadcastId = message.id;
  if (options.sender.kind === "worker") {
    if (!state.concluded) countWorkerMessage(state);
    if (message.recipients.some((recipient) => recipient.id === ORCHESTRATOR_ROOM_ID)) {
      state.moderationRequired = true;
    }
  }
  return message;
}

function requireObligation(state: RoomState, messageId: string, workerId: string): RoomObligation {
  const obligation = state.obligations.find((candidate) =>
    candidate.messageId === messageId && candidate.workerId === workerId,
  );
  if (!obligation) throw new Error(`No room response obligation for ${workerId} on ${messageId}.`);
  return obligation;
}

export function markRoomDelivered(state: RoomState, messageId: string, workerId: string): void {
  const obligation = requireObligation(state, messageId, workerId);
  if (obligation.status === "pending") obligation.status = "delivered";
}

function closeSettledBroadcast(state: RoomState, messageId: string): void {
  if (state.openBroadcastId !== messageId || !roomMessageSettled(state, messageId)) return;
  state.openBroadcastId = null;
  state.moderationRequired = true;
}

export function recordRoomResponse(
  state: RoomState,
  messageId: string,
  worker: Pick<RoomParticipant, "id" | "name">,
  text: string,
  options: { id?: () => string; now?: () => number } = {},
): RoomMessage {
  const obligation = requireObligation(state, messageId, worker.id);
  if (obligation.status === "responded") {
    throw new Error(`${worker.name} already responded to room message ${messageId}.`);
  }
  if (obligation.status === "failed") {
    throw new Error(`${worker.name}'s response obligation already failed for room message ${messageId}.`);
  }
  const trimmed = text.trim();
  if (!trimmed) throw new Error("Room response cannot be empty.");

  const response = appendMessage(state, {
    id: (options.id ?? randomUUID)(),
    timestamp: (options.now ?? Date.now)(),
    sender: { id: worker.id, name: worker.name },
    recipients: [],
    text: boundedText(trimmed),
    broadcast: false,
    humanRequest: false,
    replyTo: messageId,
  });
  obligation.status = "responded";
  obligation.responseMessageId = response.id;
  delete obligation.error;
  countWorkerMessage(state);
  closeSettledBroadcast(state, messageId);
  return response;
}

export function failRoomObligation(
  state: RoomState,
  messageId: string,
  workerId: string,
  error: string,
): void {
  const obligation = requireObligation(state, messageId, workerId);
  if (obligation.status === "responded" || obligation.status === "failed") return;
  obligation.status = "failed";
  obligation.error = error;
  closeSettledBroadcast(state, messageId);
}

export function roomMessageSettled(state: RoomState, messageId: string): boolean {
  const obligations = state.obligations.filter((obligation) => obligation.messageId === messageId);
  return obligations.length > 0
    && obligations.every((obligation) => obligation.status === "responded" || obligation.status === "failed");
}

export function moderateRoom(state: RoomState, decision: "continue" | "conclude"): void {
  if (state.concluded) {
    throw new Error("Room deliberation has already concluded.");
  }
  if (decision === "conclude") {
    if (state.openBroadcastId) {
      throw new Error(`Cannot conclude while broadcast ${state.openBroadcastId} is still open.`);
    }
    const unsettled = state.obligations.find((obligation) =>
      obligation.status === "pending" || obligation.status === "delivered",
    );
    if (unsettled) {
      throw new Error(`Cannot conclude while room message ${unsettled.messageId} still requires a response.`);
    }
  }
  state.moderationRequired = false;
  state.messagesSinceModeration = 0;
  state.concluded = decision === "conclude";
}

export function configureRoomModeration(state: RoomState, moderationEvery: number): void {
  if (!Number.isInteger(moderationEvery) || moderationEvery < 1 || moderationEvery > 100) {
    throw new Error("Room moderation interval must be between 1 and 100 worker messages.");
  }
  state.moderationEvery = moderationEvery;
  if (state.messagesSinceModeration >= moderationEvery) state.moderationRequired = true;
}

export function resolveHumanRequest(state: RoomState, messageId: string): boolean {
  const message = state.messages.find((candidate) => candidate.id === messageId);
  if (!message || !message.humanRequest) throw new Error(`Unknown #human request: ${messageId}`);
  if (message.resolved) return false;
  message.resolved = true;
  return true;
}

export function pendingHumanRequests(state: RoomState): RoomMessage[] {
  return state.messages.filter((message) => message.humanRequest && !message.resolved);
}

export function roomObligationsForMessage(state: RoomState, messageId: string): RoomObligation[] {
  return state.obligations.filter((obligation) => obligation.messageId === messageId);
}

export function unreadRoomMessages(
  state: RoomState,
  ownerId: string,
  options: { after?: number; limit?: number; advance?: boolean } = {},
): { cursor: number; messages: RoomMessage[] } {
  const after = options.after ?? state.cursors[ownerId] ?? 0;
  const limit = options.limit ?? 50;
  if (!Number.isInteger(after) || after < 0) {
    throw new Error("Room read cursor must be a non-negative integer.");
  }
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
    throw new Error("Room read limit must be between 1 and 100.");
  }
  const messages = state.messages.filter((message) => message.sequence > after).slice(0, limit);
  const cursor = messages.at(-1)?.sequence ?? after;
  const shouldAdvance = options.advance ?? (options.after === undefined);
  if (shouldAdvance) {
    state.cursors[ownerId] = Math.max(state.cursors[ownerId] ?? 0, cursor);
  }
  return { cursor, messages };
}
