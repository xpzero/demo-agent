/** 运行日志：后端 /api/logs 增量拉取 + 轮询。 */

export type RunLogEntry = {
  id: number;
  ts: number;
  level: "info" | "warn" | "error";
  source: string;
  message: string;
  message_id?: number;
  session?: string;
  chars?: number;
};

export async function fetchRunLogs(after: number, signal?: AbortSignal): Promise<RunLogEntry[]> {
  const { API_BASE, responseError } = await import("./transport");
  const response = await fetch(`${API_BASE}/api/logs?after=${after}`, { signal });
  if (!response.ok) {
    throw await responseError(response);
  }
  const data = (await response.json()) as { entries: RunLogEntry[] };
  return data.entries;
}
