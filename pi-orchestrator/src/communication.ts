import { randomUUID } from "node:crypto";

export type Delivery = "steer" | "followUp";

export interface PromptOptions {
  streamingBehavior?: Delivery;
  preflightResult?: (accepted: boolean) => void;
}

export interface DispatchSession {
  readonly isStreaming: boolean;
  prompt(text: string, options?: PromptOptions): Promise<void>;
  steer(text: string): Promise<void>;
  followUp(text: string): Promise<void>;
}

export interface DispatchAcknowledgement {
  delivery: "immediate" | Delivery;
  accepted: true;
}

export interface WaitSession {
  waitForIdle(): Promise<void>;
}

export interface SessionEntryLike {
  type: string;
  id: string;
  timestamp: string;
  message?: unknown;
}

export interface ReadSession {
  readonly sessionManager: {
    getBranch(): readonly SessionEntryLike[];
  };
}

export interface StructuredWorkerMessage {
  id: string;
  timestamp: string;
  message: unknown;
}

export interface ReadWorkerResult {
  cursor: string | null;
  messages: StructuredWorkerMessage[];
}

export interface CursorSession {
  readonly sessionManager: {
    getLeafId(): string | null;
  };
}

export interface DispatchReceipt {
  dispatchId: string;
  after: string | null;
}

export function captureDispatchReceipt(
  session: CursorSession,
  id: () => string = randomUUID,
): DispatchReceipt {
  return {
    dispatchId: id(),
    after: session.sessionManager.getLeafId(),
  };
}

export function countUnreadResponses(session: ReadSession, after: string | null): number {
  const branch = session.sessionManager.getBranch();
  let start = 0;
  if (after !== null) {
    const cursorIndex = branch.findIndex((entry) => entry.id === after);
    if (cursorIndex < 0) throw new Error(`Unknown worker message cursor: ${after}`);
    start = cursorIndex + 1;
  }
  return branch.slice(start).filter((entry) => {
    if (entry.type !== "message" || !entry.message || typeof entry.message !== "object") return false;
    const message = entry.message as { role?: unknown; stopReason?: unknown };
    return message.role === "assistant" && message.stopReason !== "toolUse";
  }).length;
}

export async function waitForWorker(session: WaitSession): Promise<void> {
  await session.waitForIdle();
}

export function readWorkerMessages(
  session: ReadSession,
  options: { after?: string; limit?: number } = {},
): ReadWorkerResult {
  const branch = session.sessionManager.getBranch();
  let start = 0;
  if (options.after !== undefined) {
    const cursorIndex = branch.findIndex((entry) => entry.id === options.after);
    if (cursorIndex < 0) throw new Error(`Unknown worker message cursor: ${options.after}`);
    start = cursorIndex + 1;
  }
  const limit = options.limit ?? 20;
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
    throw new Error("Read limit must be between 1 and 100.");
  }
  const messages = branch
    .slice(start)
    .filter((entry) => entry.type === "message" && entry.message !== undefined)
    .slice(0, limit)
    .map((entry) => ({ id: entry.id, timestamp: entry.timestamp, message: entry.message }));
  return {
    cursor: messages.at(-1)?.id ?? options.after ?? null,
    messages,
  };
}

export function readWorkerMessagesWithRecovery(
  session: ReadSession,
  after: string | null,
  limit?: number,
): ReadWorkerResult {
  const options: { after?: string; limit?: number } = {};
  if (after !== null) options.after = after;
  if (limit !== undefined) options.limit = limit;
  try {
    return readWorkerMessages(session, options);
  } catch (error) {
    const staleCursor = after !== null
      && error instanceof Error
      && error.message.startsWith("Unknown worker message cursor:");
    if (!staleCursor) throw error;
    // A native /new replaces the branch, invalidating old entry cursors.
    return readWorkerMessages(session, options.limit !== undefined ? { limit: options.limit } : {});
  }
}

export async function dispatchToWorker(
  session: DispatchSession,
  message: string,
  delivery: Delivery = "followUp",
  callbacks: {
    onBackgroundError?: (error: Error) => void;
    onSettled?: () => void;
  } = {},
): Promise<DispatchAcknowledgement> {
  if (!message.trim()) throw new Error("Worker message cannot be empty.");
  if (session.isStreaming) {
    if (delivery === "steer") await session.steer(message);
    else await session.followUp(message);
    return { delivery, accepted: true };
  }

  return await new Promise<DispatchAcknowledgement>((resolve, reject) => {
    let preflightCompleted = false;
    const run = session.prompt(message, {
      preflightResult: (accepted) => {
        preflightCompleted = true;
        if (accepted) resolve({ delivery: "immediate", accepted: true });
        else reject(new Error("Worker rejected the prompt before delivery."));
      },
    });
    void run.then(
      () => callbacks.onSettled?.(),
      (value: unknown) => {
        const error = value instanceof Error ? value : new Error(String(value));
        if (preflightCompleted) callbacks.onBackgroundError?.(error);
        else reject(error);
        callbacks.onSettled?.();
      },
    );
  });
}
