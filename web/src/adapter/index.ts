import { API_BASE, readSse, responseError } from "./transport";
import type { AgentEvent } from "./types";

export type { AgentEvent } from "./types";

export type SessionSummary = {
  id: string;
  title: string;
  current_message_id: number | null;
  created_at: number;
  updated_at: number;
};

export type SessionMessage = {
  id: number;
  parent_id: number | null;
  role: "user" | "assistant";
  status: string;
  content: string;
  created_at: number;
  files: { id: string; filename: string }[];
};

export type SessionHistory = {
  session: SessionSummary;
  messages: SessionMessage[];
};

export async function listSessions(signal?: AbortSignal): Promise<SessionSummary[]> {
  const response = await fetch(`${API_BASE}/api/sessions`, { signal });
  if (!response.ok) throw await responseError(response);
  const data = (await response.json()) as { sessions: SessionSummary[] };
  return data.sessions;
}

/** 网络流在收到 user_message 前断开时，恢复服务端已持久化的父消息指针。 */
export async function getSessionHistory(
  sessionId: string,
  signal?: AbortSignal,
): Promise<SessionHistory | null> {
  const response = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}`, { signal });
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw await responseError(response);
  }
  return (await response.json()) as SessionHistory;
}

export type ChatPayload = {
  session_id: string;
  parent_message_id: number | null;
  message: string;
  ref_file_ids: string[];
};

/** 发送一条聊天消息，返回 SSE 事件流。 */
export async function streamChat(
  payload: ChatPayload,
  signal?: AbortSignal,
): Promise<AsyncGenerator<AgentEvent>> {
  const response = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok || !response.body) throw await responseError(response);
  return readSse(response.body);
}
