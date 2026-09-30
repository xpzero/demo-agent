import type { AgentEvent } from "./types";

export const API_BASE = "http://localhost:8000";

export class HttpError extends Error {
  status: number;
  code: string | null;
  constructor(message: string, status: number, code: string | null = null) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export async function responseError(response: Response): Promise<HttpError> {
  const data = (await response.json().catch(() => null)) as {
    detail?: string | { message?: string; code?: string };
  } | null;
  const detail = data?.detail;
  const message = typeof detail === "string" ? detail : detail?.message;
  const code = typeof detail === "string" ? null : detail?.code ?? null;
  return new HttpError(message ?? `请求失败：${response.status}`, response.status, code);
}

/** 支持 id/data/注释心跳及跨 chunk 分帧；恢复流只有 stream_end 才结束。 */
export async function* readSse(body: ReadableStream<Uint8Array>, mode: "chat" | "resume" = "chat"): AsyncGenerator<AgentEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminated = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      let match;
      while ((match = /\r?\n\r?\n/.exec(buffer)) !== null) {
        const frame = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        const data: string[] = [];
        let eventId: string | undefined;
        for (const line of frame.split(/\r?\n/)) {
          if (line.startsWith(":")) {
            continue;
          }
          const colon = line.indexOf(":");
          const field = colon < 0 ? line : line.slice(0, colon);
          const raw = colon < 0 ? "" : line.slice(colon + 1);
          const value = raw.startsWith(" ") ? raw.slice(1) : raw;
          if (field === "data") {
            data.push(value);
          } else if (field === "id" && !value.includes("\0")) {
            eventId = value;
          }
        }
        if (!data.length) {
          continue;
        }
        if (terminated) {
          throw new Error("响应格式错误：结束后仍有事件");
        }
        const event = JSON.parse(data.join("\n")) as AgentEvent;
        terminated = mode === "resume" ? event.type === "stream_end" : event.type === "done" || event.type === "error";
        yield eventId === undefined ? event : { ...event, eventId };
      }
      if (done) {
        if (buffer.trim() !== "") {
          throw new Error("响应中断：数据流在不完整的消息帧处结束");
        }
        if (!terminated) {
          throw new Error("响应在完成前中断，请重试");
        }
        break;
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
