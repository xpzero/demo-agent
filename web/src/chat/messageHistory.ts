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
          events: [
            ...(message.tool_runs ?? []).flatMap((run) => [
              { type: "tool_call" as const, id: String(run.id), name: run.name, args: run.args_excerpt, excerpt: true },
              { type: "tool_result" as const, id: String(run.id), content: run.result_excerpt, elapsed: run.duration_ms ?? undefined },
            ]),
            ...(message.content ? [{ type: "text_delta" as const, text: message.content }] : []),
            ...(!message.content && !message.tool_runs?.length
              ? [{ type: "done" as const, content: "", message_id: message.id }] : []),
          ],
        },
  );
}

export function compressionBoundaryIndex(messages: ChatMessage[], cursor: number | null): number {
  if (cursor === null) {
    return -1;
  }
  const last = messages.findLastIndex((message) => message.messageId !== undefined && message.messageId <= cursor);
  return last >= 0 && last < messages.length - 1 ? last : -1;
}

export function syncMessageTimestamps(current: ChatMessage[], messages: SessionMessage[]): ChatMessage[] {
  const createdAtById = new Map(messages.map((message) => [message.id, message.created_at]));
  return current.map((message) => {
    const createdAt = message.messageId == null ? undefined : createdAtById.get(message.messageId);
    return createdAt === undefined ? message : { ...message, createdAt };
  });
}
