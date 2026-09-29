import type { AgentEvent } from "./types";

export const API_BASE = "http://localhost:8000";

export class HttpError extends Error {
  status: number;
  /** 后端稳定错误码（如 session_busy），无码时为 null；调用方据此分支，不解析提示文本。 */
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
  const message =
    typeof detail === "string" ? detail : detail?.message;
  const code = typeof detail === "string" ? null : detail?.code ?? null;
  return new HttpError(message ?? `请求失败：${response.status}`, response.status, code);
}

/** 逐行解析 SSE：chunk 可能含多条或半条消息，按空行分帧。 */
export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<AgentEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminated = false;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        // EOF 时还剩半帧，说明流被截断，不能当正常结束。
        if (buffer.trim() !== "") {
          throw new Error("响应中断：数据流在不完整的消息帧处结束");
        }
        if (!terminated) {
          throw new Error("响应在完成前中断，请重试");
        }
        break;
      }

      buffer += decoder.decode(value, { stream: true });

      let separator;
      while ((separator = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, separator);
        buffer = buffer.slice(separator + 2);
        if (frame.startsWith("data: ")) {
          if (terminated) {
            throw new Error("响应格式错误：结束后仍有事件");
          }
          const event = JSON.parse(frame.slice(6)) as AgentEvent;
          terminated = event.type === "done" || event.type === "error";
          yield event;
        }
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
