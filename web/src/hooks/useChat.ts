import { useRef, useState } from "react";
import { streamChat, type AgentEvent } from "@/adapter";

/** 一条聊天记录：用户输入原文，或助手回合的原始事件流。 */
export type ChatEntry =
  | { kind: "user"; id: string; text: string }
  | { kind: "assistant"; id: string; events: AgentEvent[] };

/**
 * 页内聊天历史：entries 数组顺序即时间序。
 * runningRef 守卫保证同一时刻最多一条进行中的流，流式事件恒定追加进最后一条 assistant 条目。
 */
export function useChat() {
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const runningRef = useRef(false);

  const send = async (message: string) => {
    if (runningRef.current) {
      return;
    }
    runningRef.current = true;
    setRunning(true);
    setError("");
    setEntries((prev) => [
      ...prev,
      { kind: "user", id: crypto.randomUUID(), text: message },
      { kind: "assistant", id: crypto.randomUUID(), events: [] },
    ]);
    try {
      for await (const event of await streamChat(message)) {
        console.log("dev: event", event);
        setEntries((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.kind === "assistant") {
            next[next.length - 1] = {
              ...last,
              events: [...last.events, event],
            };
          }
          return next;
        });
        if (event.type === "error") {
          setError(event.message);
        }
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      runningRef.current = false;
      setRunning(false);
    }
  };

  return { running, error, entries, send };
}
