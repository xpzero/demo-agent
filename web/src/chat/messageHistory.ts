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
          ...(message.status === "incomplete" ? { incomplete: true } : {}),
          events: [
            ...(message.tool_runs ?? []).flatMap((run) => [
              { type: "tool_call" as const, id: String(run.id), name: run.name, args: run.args_excerpt, excerpt: true },
              { type: "tool_result" as const, id: String(run.id), content: run.result_excerpt, elapsed: run.duration_ms ?? undefined },
            ]),
            ...(message.content ? [{ type: "text_delta" as const, text: message.content }] : []),
            ...(!message.content && !message.tool_runs?.length && message.status !== "incomplete"
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

/**
 * 终止事件（done/error）后用服务端落库事实校准本轮：正文、完成状态、工具存档
 * 与消息 ID 以历史记录为准，替换本地流式展示的临时回合。找不到对应落库记录时
 * 保留本地展示，不伪造服务端事实。
 */
export function reconcileTurnFromHistory(
  current: ChatMessage[],
  history: SessionMessage[],
  ids: { userId: string; assistantId: string },
): ChatMessage[] {
  const userIndex = current.findIndex((message) => message.id === ids.userId);
  if (userIndex === -1) {
    return current;
  }
  const user = current[userIndex];
  if (user.kind !== "user") {
    return current;
  }
  const assistant = current[userIndex + 1];
  if (assistant?.kind !== "assistant" || assistant.id !== ids.assistantId) {
    return current;
  }
  const persistedUser = user.messageId == null ? undefined : history.find((message) => message.id === user.messageId);
  if (!persistedUser) {
    return current;
  }
  // 同一父消息可能存在中断保存的未完成记录；优先取已完成/失败的那条。
  const candidates = history.filter((message) => message.role === "assistant" && message.parent_id === persistedUser.id);
  const persisted = candidates.find((message) => message.status !== "incomplete") ?? candidates.at(-1);
  if (!persisted) {
    return current;
  }
  const [reconciledUser, reconciledAssistant] = historyToMessages([persistedUser, persisted]);
  const next = [...current];
  next[userIndex] = reconciledUser;
  next[userIndex + 1] = reconciledAssistant;
  return next;
}

/** 保留完整流的事件顺序，只以历史同步持久化元数据；事实降级仍用历史展示。 */
export function calibrateHistory(current: ChatMessage[], history: SessionMessage[], ids: { userId: string; assistantId: string } | null): ChatMessage[] {
  const fallback = historyToMessages(history);
  if (!ids) {
    return fallback;
  }
  const user = current.find((message) => message.id === ids.userId);
  const assistant = current.find((message) => message.id === ids.assistantId);
  if (!user || user.kind !== "user" || !assistant || assistant.kind !== "assistant") {
    return fallback;
  }
  const persisted = history.findLast((message) => message.role === "assistant" && message.parent_id === user.messageId);
  if (!persisted) {
    return fallback;
  }
  const text = assistant.events.filter((event) => event.type === "text_delta").map((event) => event.text).join("");
  if (text !== persisted.content) {
    return fallback;
  }
  return fallback.map((message) => message.messageId === persisted.id ? {
    ...assistant, messageId: persisted.id, createdAt: persisted.created_at,
    incomplete: persisted.status === "incomplete",
  } : message);
}

export function syncMessageTimestamps(current: ChatMessage[], messages: SessionMessage[]): ChatMessage[] {
  const createdAtById = new Map(messages.map((message) => [message.id, message.created_at]));
  return current.map((message) => {
    const createdAt = message.messageId == null ? undefined : createdAtById.get(message.messageId);
    return createdAt === undefined ? message : { ...message, createdAt };
  });
}
