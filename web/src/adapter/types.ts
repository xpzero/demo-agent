/** 后端事件协议：与 server/agent/loop.py 产出的事件一一对应。 */
export type AgentEvent =
  | { type: "user_message"; message_id: number }
  | { type: "text_delta"; text: string }
  | { type: "tool_call"; id: string; name: string; args: { [key: string]: unknown } | string; excerpt?: boolean }
  | { type: "tool_result"; id: string; content: string; elapsed?: number }
  | { type: "done"; content: string; message_id: number }
  | { type: "max_turns" }
  | { type: "error"; message: string };
