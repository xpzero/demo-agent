import type { AgentEvent } from "@/adapter";
import type { ChatMessage } from "./message.ts";

export type TurnIds = { userId: string; assistantId: string };

/** 本地 ID 保持消息在流式更新时的渲染 key 稳定；落库 ID 另存为 messageId。 */
export function appendPendingTurn(
  messages: ChatMessage[],
  text: string,
  ids: TurnIds,
  createdAt: number,
): ChatMessage[] {
  return [
    ...messages,
    { kind: "user", id: ids.userId, text, createdAt },
    { kind: "assistant", id: ids.assistantId, events: [], createdAt },
  ];
}

export function applyChatEvent(
  messages: ChatMessage[],
  event: AgentEvent,
  ids: TurnIds,
  createdAt: number,
): ChatMessage[] {
  const next = [...messages];
  if (event.type === "user_message") {
    const userIndex = next.findIndex((message) => message.id === ids.userId);
    if (userIndex !== -1) {
      const userMessage = next[userIndex];
      if (userMessage.kind === "user") {
        next[userIndex] = { ...userMessage, messageId: event.message_id };
      }
    }
  }
  const lastIndex = next.length - 1;
  const assistant = next[lastIndex];
  if (assistant?.kind === "assistant" && assistant.id === ids.assistantId) {
    next[lastIndex] = {
      ...assistant,
      events: [...assistant.events, event],
      ...(event.type === "done" && event.content
        ? { messageId: event.message_id, createdAt }
        : {}),
    };
  }
  return next;
}
