import { useEffect, useState } from "react";
import { getSessionHistory } from "@/adapter";
import type { ChatMessage } from "@/chat/message";
import { historyToMessages } from "@/chat/messageHistory";
import { useSessionStore } from "@/stores/session";

type HistoryState = {
  generation: number;
  status: "loading" | "ready" | "error";
  error: string;
};

/** 唯一的消息数组所有者：加载当前会话并同步后续聊天使用的父消息指针。 */
export function useSessionMessages(sessionId: string, sessionGeneration: number) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [state, setState] = useState<HistoryState>({ generation: -1, status: "loading", error: "" });
  const [retryCount, setRetryCount] = useState(0);
  const setCurrentMessageId = useSessionStore((store) => store.setCurrentMessageId);

  useEffect(() => {
    const controller = new AbortController();
    setMessages([]);
    setState({ generation: sessionGeneration, status: "loading", error: "" });

    if (useSessionStore.getState().isNewSession) {
      setCurrentMessageId(null);
      setState({ generation: sessionGeneration, status: "ready", error: "" });
    } else {
      void getSessionHistory(sessionId, controller.signal).then((history) => {
        if (controller.signal.aborted || useSessionStore.getState().sessionGeneration !== sessionGeneration) {
          return;
        }
        if (!history) {
          throw new Error("会话不存在");
        }
        setCurrentMessageId(history.session.current_message_id);
        setMessages(historyToMessages(history.messages));
        setState({ generation: sessionGeneration, status: "ready", error: "" });
      }).catch((cause: unknown) => {
        if (controller.signal.aborted || useSessionStore.getState().sessionGeneration !== sessionGeneration) {
          return;
        }
        setState({
          generation: sessionGeneration,
          status: "error",
          error: cause instanceof Error ? cause.message : String(cause),
        });
      });
    }

    return () => controller.abort();
  }, [sessionId, sessionGeneration, retryCount, setCurrentMessageId]);

  const current = state.generation === sessionGeneration;
  const historyReady = current && state.status === "ready";
  return {
    messages: historyReady ? messages : [],
    setMessages,
    historyReady,
    loadingHistory: !current || state.status === "loading",
    historyError: current && state.status === "error" ? state.error : "",
    retryHistory: () => setRetryCount((count) => count + 1),
  };
}
