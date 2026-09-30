/** 后端事件协议：与 server/agent/loop.py 产出的事件一一对应。 */
import type { SessionHistory } from "./index.ts";

export type AgentEvent = (
  | { type: "user_message"; message_id: number }
  | { type: "text_delta"; text: string }
  | { type: "tool_call"; id: string; name: string; args: { [key: string]: unknown } | string; excerpt?: boolean }
  | { type: "tool_result"; id: string; content: string; elapsed?: number }
  | { type: "done"; content: string; message_id: number }
  | { type: "max_turns" }
  | { type: "error"; message: string }
  | { type: "resume_snapshot"; history: SessionHistory; user_message_id: number | null; replay: boolean }
  | { type: "stream_end" }
) & { eventId?: string };
