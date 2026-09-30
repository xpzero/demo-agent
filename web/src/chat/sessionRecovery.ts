import { getSessionHistory, HttpError, streamResume } from "../adapter/index.ts";
import type { AgentEvent, SessionHistory } from "../adapter/index.ts";
import type { ChatMessage } from "./message.ts";
import { historyToMessages } from "./messageHistory.ts";
import type { TurnIds } from "./messageUpdates.ts";

export function needsResume(history: SessionHistory): boolean {
  const last = history.messages.at(-1);
  return !history.can_send_message || last?.role === "user" || (last?.role === "assistant" && last.status === "incomplete");
}

/** 从快照重建所属回合；全量回放必须从空助手开始，不能追加到历史半截文本。 */
export function resumeSnapshot(event: Extract<AgentEvent, { type: "resume_snapshot" }>): { messages: ChatMessage[]; ids: TurnIds | null } {
  const messages = historyToMessages(event.history.messages);
  if (!event.replay || event.user_message_id === null) {
    return { messages, ids: null };
  }
  const index = messages.findIndex((message) => message.kind === "user" && message.messageId === event.user_message_id);
  if (index < 0) {
    throw new Error("恢复快照缺少对应用户消息");
  }
  const persisted = event.history.messages.find((message) => message.role === "assistant" && message.parent_id === event.user_message_id);
  const existing = persisted ? messages.find((message) => message.messageId === persisted.id) : undefined;
  const assistant: ChatMessage = {
    kind: "assistant", id: existing?.id ?? `resume-${event.user_message_id}`, events: [],
    createdAt: existing?.createdAt ?? messages[index].createdAt,
    ...(persisted ? { messageId: persisted.id } : {}),
  };
  // 一次执行对应的旧助手行全部移除；其它回合不受影响。
  const oldIds = new Set(event.history.messages.filter((message) => message.role === "assistant" && message.parent_id === event.user_message_id).map((message) => message.id));
  const next = messages.filter((message) => message.messageId === undefined || !oldIds.has(message.messageId));
  next.splice(index + 1, 0, assistant);
  return { messages: next, ids: { userId: messages[index].id, assistantId: assistant.id } };
}

/** 一条连接收尾后无条件校准；新执行占位时订阅新执行，失败直接交给 UI，不重试。 */
export async function recoverSession(
  sessionId: string, signal: AbortSignal,
  onEvent: (event: AgentEvent) => void,
  onHistory: (history: SessionHistory | null) => void,
  isNewSession = false,
): Promise<void> {
  while (!signal.aborted) {
    let events;
    try {
      events = await streamResume(sessionId, signal);
    } catch (cause) {
      if (!(isNewSession && cause instanceof HttpError && cause.status === 404)) {
        throw cause;
      }
      signal.throwIfAborted();
      onHistory(null);
      return;
    }
    if (events) {
      for await (const event of events) {
        signal.throwIfAborted();
        onEvent(event);
      }
    }
    signal.throwIfAborted();
    const history = await getSessionHistory(sessionId, signal);
    signal.throwIfAborted();
    if (!history && !isNewSession) {
      throw new Error("会话不存在，已保持发送保护");
    }
    onHistory(history);
    if (!history || history.can_send_message) {
      return;
    }
  }
}
