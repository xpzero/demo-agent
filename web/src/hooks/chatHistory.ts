import type { AgentEvent, SessionMessage } from "@/adapter";

export type ChatEntry =
  | { kind: "user"; id: string; text: string; createdAt: number; messageId?: number }
  | { kind: "assistant"; id: string; events: AgentEvent[]; createdAt: number; messageId?: number };

export function historyToEntries(messages: SessionMessage[]): ChatEntry[] {
  return messages.map((message) =>
    message.role === "user"
      ? { kind: "user", id: String(message.id), text: message.content, createdAt: message.created_at, messageId: message.id }
      : {
          kind: "assistant",
          id: String(message.id),
          createdAt: message.created_at,
          messageId: message.id,
          events: message.content
            ? [{ type: "text_delta", text: message.content }]
            : [{ type: "done", content: "", message_id: message.id }],
        },
  );
}

export function syncEntryTimestamps(entries: ChatEntry[], messages: SessionMessage[]): ChatEntry[] {
  const createdAtById = new Map(messages.map((message) => [message.id, message.created_at]));
  return entries.map((entry) => {
    const createdAt = entry.messageId == null ? undefined : createdAtById.get(entry.messageId);
    return createdAt === undefined ? entry : { ...entry, createdAt };
  });
}

export function copyTextForEntry(entry: ChatEntry): string {
  if (entry.kind === "user") return entry.text;
  const done = entry.events.findLast((event) => event.type === "done");
  if (done?.type === "done" && done.content) return done.content;
  return entry.events
    .filter((event) => event.type === "text_delta")
    .map((event) => event.text)
    .join("");
}
