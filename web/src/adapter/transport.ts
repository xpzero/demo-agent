import type { AgentEvent } from "./types";

export const API_BASE = "http://localhost:8000";

export async function responseError(response: Response): Promise<Error> {
  const data = (await response.json().catch(() => null)) as {
    detail?: string | { message?: string };
  } | null;
  const detail = data?.detail;
  const message =
    typeof detail === "string" ? detail : detail?.message;
  return new Error(message ?? `请求失败：${response.status}`);
}

/** 逐行解析 SSE：chunk 可能含多条或半条消息，按空行分帧。 */
export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<AgentEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      // EOF 时还剩半帧，说明流被截断，不能当正常结束。
      if (buffer.trim() !== "") {
        throw new Error("响应中断：数据流在不完整的消息帧处结束");
      }
      break;
    }

    buffer += decoder.decode(value, { stream: true });

    let separator;
    while ((separator = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, separator);
      buffer = buffer.slice(separator + 2);
      if (frame.startsWith("data: ")) {
        yield JSON.parse(frame.slice(6)) as AgentEvent;
      }
    }
  }
}
