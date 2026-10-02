import type { AgentEvent } from "@/adapter";

export type ChatMessage =
  | { kind: "user"; id: string; text: string; createdAt: number; messageId?: number }
  | {
      kind: "assistant";
      id: string;
      events: AgentEvent[];
      createdAt: number;
      messageId?: number;
      /** 库内消息状态：unfinished 表示执行中（恢复判据）；终态消息来自历史接口。 */
      finishKind?: string;
    };

export function getMessageText(message: ChatMessage): string {
  if (message.kind === "user") return message.text;
  const done = message.events.findLast((event) => event.type === "done");
  if (done?.type === "done" && done.content) return done.content;
  return message.events
    .filter((event) => event.type === "text_delta")
    .map((event) => event.text)
    .join("");
}
