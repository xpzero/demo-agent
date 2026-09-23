import type { SessionMessage } from "@/adapter";
import type { ChatMessage } from "./message.ts";

export function historyToMessages(messages: SessionMessage[]): ChatMessage[] {
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

export function syncMessageTimestamps(current: ChatMessage[], messages: SessionMessage[]): ChatMessage[] {
  const createdAtById = new Map(messages.map((message) => [message.id, message.created_at]));
  return current.map((message) => {
    const createdAt = message.messageId == null ? undefined : createdAtById.get(message.messageId);
    return createdAt === undefined ? message : { ...message, createdAt };
  });
}
