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
  /** 按本轮用户消息关联的工具账本（仅含存档短文本）。 */
  tool_runs?: { id: number; name: string; args_excerpt: string; result_excerpt: string; duration_ms: number | null }[];
  created_at: number;
  files: { id: string; filename: string }[];
};

export type SessionHistory = {
  session: SessionSummary & {
    summary: string | null;
    summary_upto_message_id: number | null;
  };
  messages: SessionMessage[];
};

/** 会话级观测汇总（GET /api/sessions/{id}/stats 现场聚合）。 */
export type SessionStats = {
  turns: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  estimated_prompt_tokens: number;
  estimated_completion_tokens: number;
  estimated: boolean;
};

/** 拉取会话观测汇总；会话不存在返回 null。 */
export async function getSessionStats(
  sessionId: string,
  signal?: AbortSignal,
): Promise<SessionStats | null> {
  const response = await fetch(
    `${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/stats`,
    { signal },
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw await responseError(response);
  }
  return (await response.json()) as SessionStats;
}

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
