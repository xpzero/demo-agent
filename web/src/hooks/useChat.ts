import { useRef, useState } from "react";
import { getSessionHistory, streamChat, type AgentEvent } from "@/adapter";
import { useSessionStore } from "@/stores/session";

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
  const recoveryNeededRef = useRef(false);
  const setCurrentMessageId = useSessionStore(
    (state) => state.setCurrentMessageId,
  );

  const send = async (message: string, refFileIds: string[] = []) => {
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
      const { sessionId } = useSessionStore.getState();
      if (recoveryNeededRef.current) {
        const history = await getSessionHistory(sessionId);
        if (history?.session.current_message_id != null) {
          setCurrentMessageId(history.session.current_message_id);
        }
        recoveryNeededRef.current = false;
      }
      const parentMessageId = useSessionStore.getState().currentMessageId;
      for await (const event of await streamChat({
        session_id: sessionId,
        parent_message_id: parentMessageId,
        message,
        ref_file_ids: refFileIds,
      })) {
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
        if (event.type === "user_message" || event.type === "done") {
          setCurrentMessageId(event.message_id);
          recoveryNeededRef.current = false;
        }
      }
    } catch (cause) {
      recoveryNeededRef.current = true;
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      // 请求可能已在后端创建用户消息，但 SSE 首帧未抵达浏览器。
      const sessionId = useSessionStore.getState().sessionId;
      // 失败也要在气泡里留下可见痕迹，不能永远停在「思考中」。
      setEntries((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.kind === "assistant") {
          next[next.length - 1] = {
            ...last,
            events: [...last.events, { type: "error", message }],
          };
        }
        return next;
      });
      try {
        const history = await getSessionHistory(sessionId);
        if (history?.session.current_message_id != null) {
          setCurrentMessageId(history.session.current_message_id);
        }
        recoveryNeededRef.current = false;
      } catch {
        // 保留原始网络错误；下次发送前重试恢复指针。
      }
    } finally {
      runningRef.current = false;
      setRunning(false);
    }
  };

  return { running, error, entries, send };
}
