import { useEffect, useState } from "react";
import { getSessionHistory, type SessionHistory } from "@/adapter";
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
  const [summaryCursor, setSummaryCursor] = useState<number | null>(null);
  const [state, setState] = useState<HistoryState>({ generation: -1, status: "loading", error: "" });
  const [retryCount, setRetryCount] = useState(0);
  const [canSend, setCanSend] = useState(false);
  const [initialHistory, setInitialHistory] = useState<SessionHistory | null>(null);
  const setCurrentMessageId = useSessionStore((store) => store.setCurrentMessageId);

  useEffect(() => {
    const controller = new AbortController();
    setMessages([]);
    setInitialHistory(null);
    setCanSend(false);
    setSummaryCursor(null);
    setState({ generation: sessionGeneration, status: "loading", error: "" });

    if (useSessionStore.getState().isNewSession && !useSessionStore.getState().restorePending) {
      setCurrentMessageId(null);
      setCanSend(true);
      setState({ generation: sessionGeneration, status: "ready", error: "" });
    } else {
      void getSessionHistory(sessionId, controller.signal).then((history) => {
        if (controller.signal.aborted || useSessionStore.getState().sessionGeneration !== sessionGeneration) {
          return;
        }
        if (!history) {
          if (useSessionStore.getState().restorePending) {
            setCurrentMessageId(null);
            setCanSend(true);
            setState({ generation: sessionGeneration, status: "ready", error: "" });
            return;
          }
          throw new Error("会话不存在");
        }
        setInitialHistory(history);
        setCurrentMessageId(history.session.current_message_id);
        setSummaryCursor(history.session.summary_upto_message_id);
        setMessages(historyToMessages(history.messages));
        setCanSend(history.can_send_message);
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
    summaryCursor: historyReady ? summaryCursor : null,
    setSummaryCursor,
    setMessages,
    canSend: historyReady && canSend,
    setCanSend,
    historyReady,
    initialHistory,
    loadingHistory: !current || state.status === "loading",
    historyError: current && state.status === "error" ? state.error : "",
    retryHistory: () => setRetryCount((count) => count + 1),
  };
}
