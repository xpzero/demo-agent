import type { ChatModelAdapter, ThreadAssistantMessagePart } from "@assistant-ui/react";

const API_BASE = "http://localhost:8000";

/** 页面生命周期内复用同一条会话，首次发消息时创建 */
let sessionIdPromise: Promise<number> | null = null;

const getSessionId = (): Promise<number> => {
  sessionIdPromise ??= fetch(`${API_BASE}/api/sessions`, { method: "POST" })
    .then(res => res.json())
    .then((data: { id: number }) => data.id);
  return sessionIdPromise;
};

/** 后端 SSE 事件，与 server/agent/loop.py 的 stream_events 一一对应 */
type AgentEvent =
  | { type: "text_delta"; text: string }
  | { type: "tool_call"; id: string; name: string; args: Record<string, unknown> }
  | { type: "tool_result"; id: string; content: string }
  | { type: "done"; content: string }
  | { type: "max_turns" };

/** 逐行解析 SSE：chunk 可能含多条或半条消息，按空行分帧 */
async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<AgentEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    let sep;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      if (frame.startsWith("data: ")) {
        yield JSON.parse(frame.slice(6)) as AgentEvent;
      }
    }
  }
}

export const chatAdapter: ChatModelAdapter = {
  async *run({ messages, abortSignal }) {
    const last = messages.at(-1);
    const text =
      last?.content.find(part => part.type === "text")?.text ?? "";

    const sessionId = await getSessionId();
    const response = await fetch(`${API_BASE}/api/sessions/${sessionId}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
      signal: abortSignal,
    });
    if (!response.ok || !response.body) {
      throw new Error(`请求失败：${response.status}`);
    }

    // assistant-ui 约定每次 yield 的是当前累积的完整 content，而不是增量
    const parts: ThreadAssistantMessagePart[] = [];
    let currentText = "";

    for await (const event of readSse(response.body)) {
      if (event.type === "text_delta") {
        currentText += event.text;
        // 文本 part 始终只有一个，放在末尾（工具调用之后）
        const withoutText = parts.filter(part => part.type !== "text");
        parts.length = 0;
        parts.push(...withoutText, { type: "text", text: currentText });
      } else if (event.type === "tool_call") {
        parts.push({
          type: "tool-call",
          toolCallId: event.id,
          toolName: event.name,
          args: event.args,
        } as ThreadAssistantMessagePart);
      } else if (event.type === "tool_result") {
        const index = parts.findIndex(
          part => part.type === "tool-call" && part.toolCallId === event.id,
        );
        if (index !== -1) {
          parts[index] = { ...parts[index], result: event.content } as ThreadAssistantMessagePart;
        }
      } else if (event.type === "max_turns") {
        parts.push({ type: "text", text: "[达到最大轮次，停止]" });
      }

      yield { content: [...parts] };
    }
  },
};
