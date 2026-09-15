import { API_BASE, readSse, responseError } from "./transport";
import type { AgentEvent } from "./types";

export type { AgentEvent } from "./types";

/** 发送一条聊天消息，返回 SSE 事件流。 */
export async function streamChat(
  message: string,
  signal?: AbortSignal,
): Promise<AsyncGenerator<AgentEvent>> {
  const response = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
    signal,
  });
  if (!response.ok || !response.body) throw await responseError(response);
  return readSse(response.body);
}
