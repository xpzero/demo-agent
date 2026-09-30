import type { AgentEvent } from "@/adapter";

export type ToolSegment = {
  kind: "tool";
  id: string;
  name: string;
  args: { [key: string]: unknown } | string | null;
  output: string | null;
  excerpt: boolean;
  /** 工具执行耗时（毫秒），结果未返回时为 null。 */
  elapsed: number | null;
};

export type Segment =
  | { kind: "text"; text: string }
  | ToolSegment
  | { kind: "note"; text: string };

/** 工具返回后模型还需再次请求；只有未完成的实时消息才显示等待提示。 */
export function isAwaitingReply(events: AgentEvent[], completed: boolean): boolean {
  return !completed && (events.length === 0 || events.at(-1)?.type === "tool_result");
}

/** 按事件到达序派生展示分段：文本增量累加，工具调用截断文本段，结果按 tool_call_id 回填。 */
export function deriveSegments(events: AgentEvent[]): Segment[] {
  const segments: Segment[] = [];
  for (const event of events) {
    const last = segments[segments.length - 1];
    if (event.type === "text_delta") {
      if (last && last.kind === "text") {
        last.text += event.text;
      } else {
        segments.push({ kind: "text", text: event.text });
      }
    } else if (event.type === "tool_call") {
      segments.push({
        kind: "tool", id: event.id, name: event.name,
        args: event.args, output: null, elapsed: null, excerpt: event.excerpt ?? false,
      });
    } else if (event.type === "tool_result") {
      const tool = segments.find((item): item is ToolSegment => item.kind === "tool" && item.id === event.id);
      if (tool) {
        tool.output = event.content;
        tool.elapsed = event.elapsed ?? null;
      }
    } else if (event.type === "max_turns") {
      segments.push({ kind: "note", text: "已达到最大轮次" });
    } else if (event.type === "done" && !event.content) {
      segments.push({ kind: "note", text: "模型返回了空回复" });
    } else if (event.type === "error") {
      segments.push({ kind: "note", text: `出错了：${event.message}` });
    }
  }
  return segments;
}
