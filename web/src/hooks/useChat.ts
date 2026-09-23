import { useEffect, useRef, useState } from "react";
import { getSessionHistory, streamChat } from "@/adapter";
import { useSessionStore } from "@/stores/session";
import { syncMessageTimestamps } from "@/chat/messageHistory";
import { appendPendingTurn, applyChatEvent, type TurnIds } from "@/chat/messageUpdates";
import { useSessionMessages } from "./useSessionMessages";

/**
 * 页内消息状态和发送流程；runningRef 保证同一时刻最多一条进行中的流。
 */
export function useChat(onConversationChanged: () => void) {
  const sessionId = useSessionStore((state) => state.sessionId);
  const sessionGeneration = useSessionStore((state) => state.sessionGeneration);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const runningRef = useRef(false);
  const recoveryNeededRef = useRef(false);
  const setCurrentMessageId = useSessionStore((state) => state.setCurrentMessageId);

  const { messages, setMessages, historyReady, loadingHistory, historyError, retryHistory } =
    useSessionMessages(sessionId, sessionGeneration);

  useEffect(() => {
    setError("");
    recoveryNeededRef.current = false;
  }, [sessionGeneration]);

  const send = async (message: string, refFileIds: string[] = []) => {
    if (runningRef.current || !historyReady || useSessionStore.getState().sessionGeneration !== sessionGeneration) {
      return;
    }
    runningRef.current = true;
    setRunning(true);
    setError("");
    const pendingSession = useSessionStore.getState().beginSession(message);
    const ids: TurnIds = { userId: crypto.randomUUID(), assistantId: crypto.randomUUID() };
    setMessages((prev) => appendPendingTurn(prev, message, ids, Date.now() / 1000));
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
        setMessages((prev) => applyChatEvent(prev, event, ids, Date.now() / 1000));
        if (event.type === "error") {
          setError(event.message);
        }
        if (event.type === "user_message" || event.type === "done") {
          setCurrentMessageId(event.message_id);
          recoveryNeededRef.current = false;
        }
      }
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      // 请求可能已在后端创建用户消息，但 SSE 首帧未抵达浏览器；
      // 标记待恢复，下次发送前用历史接口对齐父消息指针。
      recoveryNeededRef.current = true;
      // 失败也要在气泡里留下可见痕迹，不能永远停在「思考中」。
      setMessages((prev) => applyChatEvent(prev, { type: "error", message }, ids, Date.now() / 1000));
    } finally {
      runningRef.current = false;
      setRunning(false);
      onConversationChanged();
      void getSessionHistory(sessionId).then((history) => {
        if (useSessionStore.getState().sessionGeneration !== sessionGeneration) {
          return;
        }
        if (!history) {
          if (pendingSession) {
            useSessionStore.getState().discardPendingSession(pendingSession);
          }
          return;
        }
        setMessages((prev) => syncMessageTimestamps(prev, history.messages));
      }).catch(() => {
        // Local timestamps remain visible if the reconciliation request fails.
      });
    }
  };

  return {
    running,
    error,
    messages,
    send,
    historyReady,
    loadingHistory,
    historyError,
    retryHistory: () => {
      setError("");
      retryHistory();
    },
  };
}
