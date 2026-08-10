export interface TranscriptEntry {
  id: string;
  timestamp: string;
  message: unknown;
}

export interface TranscriptMessage {
  sender: string;
  receiver: string;
  text: string;
}

function textContent(content: unknown): string {
  if (typeof content === "string") return content.trim();
  if (!Array.isArray(content)) return "";
  return content
    .filter((block): block is { type: "text"; text: string } =>
      Boolean(block) && typeof block === "object" &&
      (block as { type?: unknown }).type === "text" &&
      typeof (block as { text?: unknown }).text === "string",
    )
    .map((block) => block.text.trim())
    .filter(Boolean)
    .join("\n");
}

export function transcriptMessages(
  workerName: string,
  entries: readonly TranscriptEntry[],
): TranscriptMessage[] {
  const messages: TranscriptMessage[] = [];
  for (const entry of entries) {
    if (!entry.message || typeof entry.message !== "object") continue;
    const message = entry.message as { role?: unknown; content?: unknown };
    const text = textContent(message.content);
    if (!text) continue;
    if (message.role === "user") {
      messages.push({ sender: "orchestrator", receiver: workerName, text });
    } else if (message.role === "assistant") {
      messages.push({ sender: workerName, receiver: "orchestrator", text });
    }
  }
  return messages;
}
